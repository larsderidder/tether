"""Incremental cache for pi session transcript details.

The external session watcher polls attached pi sessions every few seconds.
Re-parsing multi-hundred-MB JSONL transcripts on every poll dominates CPU
usage, so this module keeps one append-only parse cursor per session file
and decodes only newly appended bytes. An unchanged transcript costs a
single stat call.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent_sessions.models import RunnerType, SessionDetail, SessionMessage
from agent_sessions.providers.pi import (
    _decode_directory_name,
    _extract_assistant_content,
    _extract_user_text,
    _find_session_file,
)
from agent_sessions.running import find_running_pi_sessions

_MAX_CACHED_SESSIONS = 512
_MAX_CACHED_PATHS = 1024
_RUNNING_CACHE_TTL_SECONDS = 15.0


@dataclass
class _PiTranscriptState:
    """Append-only parse cursor for one pi session JSONL file."""

    path: Path
    ino: int
    size: int
    mtime_ns: int
    offset: int = 0
    pending: bytes = b""
    messages: list[SessionMessage] = field(default_factory=list)
    first_prompt: str | None = None
    last_prompt: str | None = None
    last_activity: str | None = None
    directory: str | None = None


class _PiDetailCache:
    """Thread-safe cache of parsed pi transcripts keyed by session id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paths: dict[str, Path] = {}
        self._states: dict[str, _PiTranscriptState] = {}
        self._running_at: float = 0.0
        self._running: set[str] = set()

    def clear(self) -> None:
        """Drop all cached paths, transcript states, and running detection."""
        with self._lock:
            self._paths.clear()
            self._states.clear()
            self._running_at = 0.0
            self._running = set()

    def get(self, session_id: str, limit: int) -> SessionDetail | None:
        """Return session detail, parsing only bytes appended since the last call."""
        with self._lock:
            path = self._resolve_path(session_id)
            if path is None:
                return None
            try:
                stat = path.stat()
            except OSError:
                self._states.pop(session_id, None)
                return None
            state = self._states.get(session_id)
            if state is not None and (
                state.path != path
                or state.ino != stat.st_ino
                or stat.st_size < state.offset
            ):
                # File replaced or truncated: reparse from the start.
                state = None
            if (
                state is not None
                and stat.st_size == state.size
                and stat.st_mtime_ns == state.mtime_ns
            ):
                return self._build_detail(session_id, state, limit)
            if state is None:
                state = _PiTranscriptState(
                    path=path,
                    ino=stat.st_ino,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                )
                self._states[session_id] = state
            else:
                state.size = stat.st_size
                state.mtime_ns = stat.st_mtime_ns
            if stat.st_size > state.offset:
                with open(path, "rb") as handle:
                    handle.seek(state.offset)
                    data = handle.read()
                state.offset += len(data)
                self._parse_chunk(state, data)
            self._evict()
            return self._build_detail(session_id, state, limit)

    def _resolve_path(self, session_id: str) -> Path | None:
        """Return the cached session file, resolving it once per session id."""
        path = self._paths.get(session_id)
        if path is not None and path.exists():
            return path
        path = _find_session_file(session_id)
        if path is None:
            self._paths.pop(session_id, None)
            return None
        self._paths[session_id] = path
        while len(self._paths) > _MAX_CACHED_PATHS:
            self._paths.pop(next(iter(self._paths)))
        return path

    def _evict(self) -> None:
        while len(self._states) > _MAX_CACHED_SESSIONS:
            self._states.pop(next(iter(self._states)))

    def _running_sessions(self) -> set[str]:
        """Return running pi session ids, refreshing the ps scan at most per TTL."""
        now = time.monotonic()
        if now - self._running_at >= _RUNNING_CACHE_TTL_SECONDS:
            self._running = find_running_pi_sessions()
            self._running_at = now
        return self._running

    def _parse_chunk(self, state: _PiTranscriptState, data: bytes) -> None:
        """Parse complete appended lines, buffering a trailing partial line."""
        chunk = state.pending + data
        cut = chunk.rfind(b"\n")
        if cut == -1:
            state.pending = chunk
            return
        state.pending = chunk[cut + 1 :]
        for raw in chunk[: cut + 1].splitlines():
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._apply_record(state, record)

    @staticmethod
    def _apply_record(state: _PiTranscriptState, record: dict) -> None:
        """Fold one JSONL record into the cached transcript state."""
        timestamp = record.get("timestamp")
        if timestamp:
            state.last_activity = timestamp
        record_type = record.get("type")
        if record_type == "session":
            cwd = record.get("cwd")
            if cwd:
                state.directory = cwd
        if record_type == "message":
            message = record.get("message", {})
            role = message.get("role")
            if role == "user":
                text = _extract_user_text(message.get("content"))
                if text:
                    if state.first_prompt is None:
                        state.first_prompt = text[:200]
                    state.last_prompt = text[:200]
                    state.messages.append(
                        SessionMessage(role="user", content=text, timestamp=timestamp)
                    )
            elif role == "assistant":
                text, thinking = _extract_assistant_content(message.get("content"))
                if text or thinking:
                    state.messages.append(
                        SessionMessage(
                            role="assistant",
                            content=text,
                            thinking=thinking,
                            timestamp=timestamp,
                        )
                    )

    def _build_detail(
        self, session_id: str, state: _PiTranscriptState, limit: int
    ) -> SessionDetail:
        """Build a SessionDetail from cached state, applying the message limit."""
        messages = state.messages
        if len(messages) > limit:
            messages = messages[-limit:]
        directory = state.directory
        if directory is None:
            directory = _decode_directory_name(state.path.parent.name)
        last_activity = state.last_activity
        if last_activity is None:
            last_activity = datetime.fromtimestamp(
                state.path.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        return SessionDetail(
            id=session_id,
            runner_type=RunnerType.PI,
            directory=directory,
            first_prompt=state.first_prompt,
            last_prompt=state.last_prompt,
            last_activity=last_activity,
            message_count=len(messages),
            is_running=session_id in self._running_sessions(),
            messages=messages,
        )


_cache = _PiDetailCache()


def get_pi_session_detail_cached(
    session_id: str, limit: int = 100
) -> SessionDetail | None:
    """Load pi session detail, parsing only transcript bytes appended since the last call."""
    return _cache.get(session_id, limit)


def clear_pi_detail_cache() -> None:
    """Drop all cached pi transcript state (used by tests)."""
    _cache.clear()
