"""Session storage and runtime process state."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from collections import deque
from dataclasses import dataclass, field
import re
from datetime import datetime, timedelta, timezone
from threading import Lock

import structlog
from sqlalchemy import func as sa_func
from sqlmodel import select

from tether.db import get_session as get_db_session
from tether.git import has_git_repository, normalize_directory_path
from tether.models import Message, RepoRef, Session, SessionState
from tether.settings import settings

logger = structlog.get_logger("tether.store")


@dataclass
class SessionRuntime:
    """Per-session runtime state (not persisted to database)."""

    seq: int = 0
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    proc: asyncio.subprocess.Process | None = None
    pending_inputs: list[str] = field(default_factory=list)
    recent_output: deque[str] = field(default_factory=lambda: deque(maxlen=10))
    claude_task: asyncio.Task | None = None
    stop_requested: bool = False
    synced_message_count: int = 0


class SessionStore:
    """Session registry with SQLModel persistence and per-session process bookkeeping."""

    def __init__(self) -> None:
        self._data_dir = settings.data_dir()
        self._db_lock = Lock()
        os.makedirs(self._data_dir, exist_ok=True)
        os.makedirs(os.path.join(self._data_dir, "sessions"), exist_ok=True)
        self._sessions: dict[str, Session] = {}
        self._runtime: dict[str, SessionRuntime] = {}
        self._load_sessions()

    def _get_runtime(self, session_id: str) -> SessionRuntime:
        """Get or create runtime state for a session."""
        if session_id not in self._runtime:
            self._runtime[session_id] = SessionRuntime()
        return self._runtime[session_id]

    def _load_sessions(self) -> None:
        with self._db_lock:
            with get_db_session() as db:
                rows = db.exec(select(Session)).all()
                for row in rows:
                    self._sessions[row.id] = row
                    self._runtime[row.id] = SessionRuntime()

    def _now(self) -> str:
        """Return an ISO8601 UTC timestamp suitable for API payloads."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _parse_ts(self, value: str) -> datetime:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )

    def create_session(self, repo_id: str, base_ref: str | None) -> Session:
        """Create and register a new session in CREATED state.

        Args:
            repo_id: Identifier for the repo being worked on.
            base_ref: Optional base ref name or branch.
        """
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        now = self._now()
        session = Session(
            id=session_id,
            repo_id=repo_id,
            repo_display=repo_id,
            repo_ref_type="path",
            repo_ref_value=repo_id,
            state=SessionState.CREATED.value,
            name="New session",
            created_at=now,
            started_at=None,
            ended_at=None,
            last_activity_at=now,
            exit_code=None,
            summary=None,
            runner_header=None,
        )
        self._sessions[session_id] = session
        self._runtime[session_id] = SessionRuntime()
        self._persist_session(session)
        return session

    def list_sessions(self) -> list[Session]:
        """Return all sessions currently tracked in memory."""
        return list(self._sessions.values())

    def get_session(self, session_id: str) -> Session | None:
        """Fetch a session by id, or None if missing."""
        return self._sessions.get(session_id)

    def update_session(self, session: Session) -> None:
        """Persist an updated session snapshot."""
        self._sessions[session.id] = session
        self._persist_session(session)

    def delete_session(self, session_id: str) -> bool:
        """Remove a session and its associated runtime state."""
        session = self._sessions.pop(session_id, None)
        if not session:
            return False
        with self._db_lock:
            with get_db_session() as db:
                # Delete messages first
                messages = db.exec(select(Message).where(Message.session_id == session_id)).all()
                for msg in messages:
                    db.delete(msg)
                # Delete session
                db_session = db.get(Session, session_id)
                if db_session:
                    db.delete(db_session)
                db.commit()
        self.clear_workdir(session_id)
        self._runtime.pop(session_id, None)
        return True

    def clear_all_data(self) -> None:
        """Delete all persisted sessions and in-memory session state."""
        with self._db_lock:
            with get_db_session() as db:
                # Delete all messages
                messages = db.exec(select(Message)).all()
                for msg in messages:
                    db.delete(msg)
                # Delete all sessions
                sessions = db.exec(select(Session)).all()
                for sess in sessions:
                    db.delete(sess)
                db.commit()
        self._sessions.clear()
        self._runtime.clear()
        logs_root = os.path.join(self._data_dir, "sessions")
        shutil.rmtree(logs_root, ignore_errors=True)
        os.makedirs(logs_root, exist_ok=True)

    def next_seq(self, session_id: str) -> int:
        """Advance and return the per-session event sequence counter."""
        runtime = self._get_runtime(session_id)
        runtime.seq += 1
        return runtime.seq

    def new_subscriber(self, session_id: str) -> asyncio.Queue:
        """Register a new SSE subscriber queue for a session.

        Args:
            session_id: Internal session identifier.
        """
        queue: asyncio.Queue = asyncio.Queue()
        runtime = self._get_runtime(session_id)
        runtime.subscribers.append(queue)
        logger.debug(
            "New SSE subscriber",
            session_id=session_id,
            total_subscribers=len(runtime.subscribers),
        )
        return queue

    def remove_subscriber(self, session_id: str, queue: asyncio.Queue) -> None:
        """Unregister an SSE subscriber queue."""
        runtime = self._runtime.get(session_id)
        if runtime and queue in runtime.subscribers:
            runtime.subscribers.remove(queue)

    async def emit(self, session_id: str, event: dict) -> None:
        """Broadcast an event payload to all session subscribers.

        Args:
            session_id: Internal session identifier.
            event: Event payload to broadcast.
        """
        runtime = self._runtime.get(session_id)
        subscribers = runtime.subscribers if runtime else []
        logger.debug(
            "Broadcasting event",
            session_id=session_id,
            event_type=event.get("type"),
            subscriber_count=len(subscribers),
        )
        for queue in list(subscribers):
            await queue.put(event)
        self._append_event_log(session_id, event)

    def _persist_session(self, session: Session) -> None:
        with self._db_lock:
            with get_db_session() as db:
                db.merge(session)
                db.commit()

    def _append_event_log(self, session_id: str, event: dict) -> None:
        path = os.path.join(self._data_dir, "sessions", session_id, "events.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        max_bytes = 5_000_000  # 5MB
        if max_bytes > 0 and os.path.exists(path):
            try:
                if os.path.getsize(path) > max_bytes:
                    rotated = f"{path}.1"
                    if os.path.exists(rotated):
                        os.remove(rotated)
                    os.replace(path, rotated)
            except OSError:
                pass
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")

    def prune_sessions(self, retention_days: int) -> int:
        """Delete sessions (and logs) older than the retention window."""
        if retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        removed = 0
        for session in list(self._sessions.values()):
            if session.state in (SessionState.RUNNING.value, SessionState.INTERRUPTING.value):
                continue
            ts = session.ended_at or session.last_activity_at or session.created_at
            if not ts:
                continue
            try:
                when = self._parse_ts(ts)
            except ValueError:
                continue
            if when < cutoff:
                if self.delete_session(session.id):
                    removed += 1
        return removed

    def set_process(self, session_id: str, proc: asyncio.subprocess.Process) -> None:
        """Track the subprocess running for a session."""
        self._get_runtime(session_id).proc = proc

    def get_process(self, session_id: str) -> asyncio.subprocess.Process | None:
        """Return the tracked subprocess, if any."""
        runtime = self._runtime.get(session_id)
        return runtime.proc if runtime else None

    def clear_process(self, session_id: str) -> None:
        runtime = self._runtime.get(session_id)
        if runtime:
            runtime.proc = None

    def add_pending_input(self, session_id: str, text: str) -> None:
        """Queue input to send once the runner is ready."""
        self._get_runtime(session_id).pending_inputs.append(text)

    def pop_pending_inputs(self, session_id: str) -> list[str]:
        """Drain and return all pending inputs."""
        runtime = self._runtime.get(session_id)
        if not runtime:
            return []
        inputs = runtime.pending_inputs[:]
        runtime.pending_inputs.clear()
        return inputs

    def clear_pending_inputs(self, session_id: str) -> None:
        runtime = self._runtime.get(session_id)
        if runtime:
            runtime.pending_inputs.clear()

    def pop_next_pending_input(self, session_id: str) -> str | None:
        """Pop the next queued input, if any."""
        runtime = self._runtime.get(session_id)
        if not runtime or not runtime.pending_inputs:
            return None
        return runtime.pending_inputs.pop(0)

    def has_pending_inputs(self, session_id: str) -> bool:
        """Return True if there is queued input."""
        runtime = self._runtime.get(session_id)
        return bool(runtime and runtime.pending_inputs)

    def set_runner_session_id(self, session_id: str, runner_session_id: str) -> None:
        """Store the runner-specific session id and persist to database."""
        session = self._sessions.get(session_id)
        if session:
            session.runner_session_id = runner_session_id
            self._persist_session(session)

    def get_runner_session_id(self, session_id: str) -> str | None:
        """Fetch the runner-specific session id."""
        session = self._sessions.get(session_id)
        return session.runner_session_id if session else None

    def clear_runner_session_id(self, session_id: str) -> None:
        """Clear the runner-specific session id."""
        session = self._sessions.get(session_id)
        if session:
            session.runner_session_id = None
            self._persist_session(session)

    def find_session_by_runner_session_id(self, runner_session_id: str) -> str | None:
        """Find a Tether session ID that is attached to the given runner session ID.

        Args:
            runner_session_id: The external/runner session ID to look up.

        Returns:
            The Tether session ID if found, None otherwise.
        """
        for session in self._sessions.values():
            if session.runner_session_id == runner_session_id:
                return session.id
        return None

    def set_synced_message_count(self, session_id: str, count: int) -> None:
        """Store the number of messages synced from external session."""
        self._get_runtime(session_id).synced_message_count = count

    def get_synced_message_count(self, session_id: str) -> int:
        """Get the number of messages previously synced from external session."""
        runtime = self._runtime.get(session_id)
        return runtime.synced_message_count if runtime else 0

    def should_emit_output(self, session_id: str, text: str) -> bool:
        """Return True if output is non-empty and not recently emitted.

        Args:
            session_id: Internal session identifier.
            text: Raw output text.
        """
        normalized = self._normalize_output(text)
        if not normalized:
            return False
        runtime = self._get_runtime(session_id)
        if normalized in runtime.recent_output:
            return False
        runtime.recent_output.append(normalized)
        return True

    def _normalize_output(self, text: str) -> str:
        """Normalize output to de-duplicate noisy repeated lines.

        Args:
            text: Output text to normalize.
        """
        # Strip ANSI codes and collapse whitespace for stable comparisons.
        stripped = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
        compact = " ".join(stripped.strip().split())
        return compact

    def clear_last_output(self, session_id: str) -> None:
        runtime = self._runtime.get(session_id)
        if runtime:
            runtime.recent_output.clear()

    def get_recent_output(self, session_id: str) -> list[str]:
        """Get recent output chunks for a session.

        Args:
            session_id: Internal session identifier.

        Returns:
            List of recent output strings (up to 10).
        """
        runtime = self._runtime.get(session_id)
        return list(runtime.recent_output) if runtime else []

    def set_workdir(self, session_id: str, path: str, *, managed: bool) -> str:
        """Record a working directory and update the session metadata."""
        normalized = normalize_directory_path(path)
        session = self._sessions.get(session_id)
        if session:
            session.directory = normalized
            session.directory_has_git = has_git_repository(normalized)
            session.workdir_managed = managed
            self.update_session(session)
        return normalized

    def create_workdir(self, session_id: str) -> str:
        """Create a temporary working directory for the session."""
        path = tempfile.mkdtemp(prefix=f"tether_{session_id}_")
        return self.set_workdir(session_id, path, managed=True)

    def get_workdir(self, session_id: str) -> str | None:
        """Return the session working directory, if set."""
        session = self._sessions.get(session_id)
        return session.directory if session else None

    def clear_workdir(self, session_id: str, *, force: bool = True) -> None:
        """Clear the working directory, removing temp dirs if managed."""
        session = self._sessions.get(session_id)
        if not session:
            return
        if not force and not session.workdir_managed:
            return
        path = session.directory
        if path and session.workdir_managed:
            shutil.rmtree(path, ignore_errors=True)
        session.directory = None
        session.workdir_managed = False

    def add_message(self, session_id: str, role: str, content: object) -> Message:
        """Add a message to conversation history.

        Args:
            session_id: Internal session identifier.
            role: Message role ("user" or "assistant").
            content: Content blocks (will be JSON-encoded).
        """
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        now = self._now()
        content_json = json.dumps(content)
        with self._db_lock:
            with get_db_session() as db:
                max_seq = db.exec(
                    select(sa_func.coalesce(sa_func.max(Message.seq), 0)).where(
                        Message.session_id == session_id
                    )
                ).one()
                seq = max_seq + 1
                message = Message(
                    id=message_id,
                    session_id=session_id,
                    role=role,
                    content=content_json,
                    created_at=now,
                    seq=seq,
                )
                db.add(message)
                db.commit()
        return Message(
            id=message_id,
            session_id=session_id,
            role=role,
            content=content_json,
            seq=seq,
            created_at=now,
        )

    def get_messages(self, session_id: str) -> list[dict]:
        """Get conversation history for a session.

        Args:
            session_id: Internal session identifier.

        Returns:
            List of message dicts with role and content for Anthropic API.
        """
        with self._db_lock:
            with get_db_session() as db:
                rows = db.exec(
                    select(Message).where(Message.session_id == session_id).order_by(Message.seq)
                ).all()
                messages = []
                for row in rows:
                    content = json.loads(row.content) if row.content else []
                    messages.append({"role": row.role, "content": content})
                return messages

    def clear_messages(self, session_id: str) -> None:
        """Clear conversation history for a session.

        Args:
            session_id: Internal session identifier.
        """
        with self._db_lock:
            with get_db_session() as db:
                messages = db.exec(select(Message).where(Message.session_id == session_id)).all()
                for msg in messages:
                    db.delete(msg)
                db.commit()

    def get_message_count(self, session_id: str) -> int:
        """Get the number of messages for a session.

        Args:
            session_id: Internal session identifier.

        Returns:
            Number of messages in the session.
        """
        with self._db_lock:
            with get_db_session() as db:
                count = db.exec(
                    select(sa_func.count(Message.id)).where(Message.session_id == session_id)
                ).one()
                return count or 0

    def set_claude_task(self, session_id: str, task: asyncio.Task) -> None:
        """Track the Claude conversation loop task for a session."""
        self._get_runtime(session_id).claude_task = task

    def get_claude_task(self, session_id: str) -> asyncio.Task | None:
        """Return the Claude conversation loop task, if any."""
        runtime = self._runtime.get(session_id)
        return runtime.claude_task if runtime else None

    def clear_claude_task(self, session_id: str) -> None:
        runtime = self._runtime.get(session_id)
        if runtime:
            runtime.claude_task = None

    def request_stop(self, session_id: str) -> None:
        """Signal the Claude conversation loop to stop."""
        self._get_runtime(session_id).stop_requested = True

    def is_stop_requested(self, session_id: str) -> bool:
        """Check if stop was requested for a session."""
        runtime = self._runtime.get(session_id)
        return runtime.stop_requested if runtime else False

    def clear_stop_requested(self, session_id: str) -> None:
        runtime = self._runtime.get(session_id)
        if runtime:
            runtime.stop_requested = False

    def read_event_log(
        self, session_id: str, *, since_seq: int = 0, limit: int | None = None
    ) -> list[dict]:
        """Read persisted SSE events for a session.

        Args:
            session_id: Internal session identifier.
            since_seq: Only return events with seq greater than this value.
            limit: Optional maximum number of events to return.
        """
        path = os.path.join(self._data_dir, "sessions", session_id, "events.jsonl")
        if not os.path.exists(path):
            return []
        events: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    seq = int(event.get("seq") or 0)
                    if seq and seq <= since_seq:
                        continue
                    events.append(event)
                    if limit and len(events) >= limit:
                        break
        except OSError:
            return []
        return events


store = SessionStore()
