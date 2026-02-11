"""Codex session discovery and parsing."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import structlog

from tether.discovery.running import find_running_codex_sessions
from tether.git import normalize_directory_path
from tether.models import (
    ExternalRunnerType,
    ExternalSessionSummary,
    ExternalSessionDetail,
    ExternalSessionMessage,
)

logger = structlog.get_logger(__name__)

_ROLLOUT_ID_RE = re.compile(r"rollout-.*-([0-9a-fA-F-]{32,})\.jsonl$")


def _codex_home() -> Path:
    """Resolve CODEX_HOME, defaulting to ~/.codex."""
    value = os.environ.get("CODEX_HOME")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".codex"


def _sessions_dir() -> Path:
    return _codex_home() / "sessions"


def _extract_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in ("input_text", "output_text", "text"):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


def _is_environment_context(text: str) -> bool:
    return text.lstrip().startswith("<environment_context>")


def _infer_session_id(session_file: Path) -> str | None:
    match = _ROLLOUT_ID_RE.match(session_file.name)
    if match:
        return match.group(1)
    return None


def _parse_session_summary(
    session_file: Path,
    running_sessions: set[str],
) -> ExternalSessionSummary | None:
    session_id: str | None = None
    first_prompt: str | None = None
    last_prompt: str | None = None
    last_activity: str | None = None
    directory: str | None = None
    message_count = 0

    try:
        with open(session_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                timestamp = record.get("timestamp")
                if timestamp:
                    last_activity = timestamp

                record_type = record.get("type")
                payload = record.get("payload", {})

                if record_type == "session_meta":
                    session_id = payload.get("id") or session_id
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str):
                        directory = cwd

                if record_type == "response_item" and isinstance(payload, dict):
                    if payload.get("type") == "message":
                        role = payload.get("role")
                        content = payload.get("content")
                        text = _extract_text(content)
                        if role in ("user", "assistant"):
                            message_count += 1
                        if role == "user" and text:
                            if not _is_environment_context(text):
                                if first_prompt is None:
                                    first_prompt = text[:200]
                                last_prompt = text[:200]

        if session_id is None:
            session_id = _infer_session_id(session_file)

        if directory is None:
            return None

        if last_activity is None:
            mtime = session_file.stat().st_mtime
            from datetime import datetime, timezone
            last_activity = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

        return ExternalSessionSummary(
            id=session_id or session_file.stem,
            runner_type=ExternalRunnerType.CODEX,
            directory=directory,
            first_prompt=first_prompt,
            last_prompt=last_prompt,
            last_activity=last_activity,
            message_count=message_count,
            is_running=(session_id or "") in running_sessions,
        )
    except Exception as exc:
        logger.warning(
            "Failed to parse Codex session file",
            session_file=str(session_file),
            error=str(exc),
        )
        return None


def list_codex_sessions(
    directory: str | None = None,
    limit: int = 50,
) -> list[ExternalSessionSummary]:
    """Discover Codex sessions stored under ~/.codex/sessions."""
    sessions_root = _sessions_dir()
    if not sessions_root.exists():
        return []

    normalized_directory = normalize_directory_path(directory) if directory else None
    running_sessions = find_running_codex_sessions()
    sessions: list[ExternalSessionSummary] = []

    for session_file in sessions_root.rglob("rollout-*.jsonl"):
        summary = _parse_session_summary(session_file, running_sessions)
        if not summary:
            continue
        if normalized_directory and normalize_directory_path(summary.directory) != normalized_directory:
            continue
        sessions.append(summary)

    sessions.sort(key=lambda s: s.last_activity, reverse=True)
    return sessions[:limit]


def _find_session_file(session_id: str) -> Path | None:
    sessions_root = _sessions_dir()
    if not sessions_root.exists():
        return None

    for session_file in sessions_root.rglob(f"*{session_id}.jsonl"):
        if session_file.is_file():
            return session_file

    # Fallback: scan for matching session_meta ID
    for session_file in sessions_root.rglob("rollout-*.jsonl"):
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                for line in f:
                    if '"type":"session_meta"' not in line:
                        continue
                    record = json.loads(line)
                    payload = record.get("payload", {})
                    if payload.get("id") == session_id:
                        return session_file
        except Exception:
            continue
    return None


def get_codex_session_detail(
    session_id: str,
    limit: int = 100,
) -> ExternalSessionDetail | None:
    session_file = _find_session_file(session_id)
    if not session_file:
        return None

    running_sessions = find_running_codex_sessions()
    first_prompt: str | None = None
    last_prompt: str | None = None
    last_activity: str | None = None
    directory: str | None = None
    messages: list[ExternalSessionMessage] = []

    try:
        with open(session_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                timestamp = record.get("timestamp")
                if timestamp:
                    last_activity = timestamp

                record_type = record.get("type")
                payload = record.get("payload", {})

                if record_type == "session_meta":
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str):
                        directory = cwd

                if record_type == "response_item" and isinstance(payload, dict):
                    if payload.get("type") != "message":
                        continue
                    role = payload.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    content = payload.get("content")
                    text = _extract_text(content)
                    if role == "user" and text:
                        if not _is_environment_context(text):
                            if first_prompt is None:
                                first_prompt = text[:200]
                            last_prompt = text[:200]
                    messages.append(
                        ExternalSessionMessage(
                            role=role,
                            content=text,
                            timestamp=timestamp,
                        )
                    )

        if directory is None:
            return None

        if last_activity is None:
            mtime = session_file.stat().st_mtime
            from datetime import datetime, timezone
            last_activity = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

        if limit and len(messages) > limit:
            messages = messages[-limit:]

        return ExternalSessionDetail(
            id=session_id,
            runner_type=ExternalRunnerType.CODEX,
            directory=directory,
            first_prompt=first_prompt,
            last_prompt=last_prompt,
            last_activity=last_activity,
            message_count=len(messages),
            is_running=session_id in running_sessions,
            messages=messages,
        )
    except Exception as exc:
        logger.warning(
            "Failed to parse Codex session detail",
            session_file=str(session_file),
            error=str(exc),
        )
        return None
