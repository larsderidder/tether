"""Pi coding agent session discovery and parsing.

Pi stores sessions under ``~/.pi/agent/sessions/`` in per-directory folders
encoded as ``--home-lars-project--``.  Each session is a JSONL file with a
``session`` header followed by tree-structured entries (messages, model changes,
compaction, etc.).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import structlog

from tether.discovery.running import find_running_pi_sessions
from tether.git import normalize_directory_path
from tether.models import (
    ExternalRunnerType,
    ExternalSessionSummary,
    ExternalSessionDetail,
    ExternalSessionMessage,
)

logger = structlog.get_logger(__name__)


def _pi_sessions_dir() -> Path:
    """Resolve the pi sessions directory."""
    value = os.environ.get("PI_SESSIONS_DIR")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".pi" / "agent" / "sessions"


def _decode_directory_name(encoded: str) -> str:
    """Convert ``--home-lars-project--`` to ``/home/lars/project``.

    Pi encodes the working directory by replacing ``/`` with ``-`` and wrapping
    with ``--``.
    """
    # Strip leading/trailing -- then replace - with /
    inner = encoded.strip("-")
    return "/" + inner.replace("-", "/")


def _encode_directory_name(path: str) -> str:
    """Convert ``/home/lars/project`` to ``--home-lars-project--``.

    Inverse of ``_decode_directory_name``.
    """
    normalized = path.lstrip("/")
    return "--" + normalized.replace("/", "-") + "--"


def _extract_user_text(content: object) -> str | None:
    """Extract text from a pi user message ``content`` field.

    Content can be a plain string or a list of content blocks.
    """
    if isinstance(content, str):
        return content.strip() or None

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        parts.append(text)
        joined = "\n".join(parts).strip()
        return joined or None

    return None


def _extract_assistant_content(content: object) -> tuple[str, str | None]:
    """Extract text and thinking from a pi assistant message ``content``.

    Returns:
        (text_content, thinking_content)
    """
    if isinstance(content, str):
        return content, None

    if isinstance(content, list):
        texts: list[str] = []
        thinking_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    texts.append(text)
            elif btype == "thinking":
                thinking = block.get("thinking", "")
                if thinking:
                    thinking_parts.append(thinking)
            elif btype == "toolCall":
                # Skip tool calls — not meaningful text
                continue

        text_content = "\n".join(texts)
        thinking_content = "\n\n".join(thinking_parts) if thinking_parts else None
        return text_content, thinking_content

    return "", None


def _parse_session_summary(
    session_file: Path,
    running_sessions: set[str],
) -> ExternalSessionSummary | None:
    """Parse a pi session JSONL file and return summary info."""
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

                record_type = record.get("type")
                timestamp = record.get("timestamp")

                if timestamp:
                    last_activity = timestamp

                # Session header
                if record_type == "session":
                    session_id = record.get("id")
                    cwd = record.get("cwd")
                    if cwd:
                        directory = cwd

                # Count user and assistant messages
                if record_type == "message":
                    message = record.get("message", {})
                    role = message.get("role")
                    if role in ("user", "assistant"):
                        message_count += 1
                    if role == "user":
                        text = _extract_user_text(message.get("content"))
                        if text:
                            if first_prompt is None:
                                first_prompt = text[:200]
                            last_prompt = text[:200]

        # Fallback: decode directory from folder name
        if directory is None:
            directory = _decode_directory_name(session_file.parent.name)

        # Fallback: use file stem as session ID
        if session_id is None:
            # File name: 2026-02-11T07-36-34-614Z_<uuid>.jsonl
            name = session_file.stem
            if "_" in name:
                session_id = name.split("_", 1)[1]
            else:
                session_id = name

        if last_activity is None:
            mtime = session_file.stat().st_mtime
            from datetime import datetime, timezone

            last_activity = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

        return ExternalSessionSummary(
            id=session_id,
            runner_type=ExternalRunnerType.PI,
            directory=directory,
            first_prompt=first_prompt,
            last_prompt=last_prompt,
            last_activity=last_activity,
            message_count=message_count,
            is_running=session_id in running_sessions,
        )

    except Exception as e:
        logger.warning(
            "Failed to parse pi session file",
            session_file=str(session_file),
            error=str(e),
        )
        return None


def list_pi_sessions(
    directory: str | None = None,
    limit: int = 50,
) -> list[ExternalSessionSummary]:
    """Discover pi coding agent sessions.

    Args:
        directory: Filter to sessions for this project directory.
        limit: Maximum sessions to return.

    Returns:
        List of session summaries sorted by last_activity descending.
    """
    sessions_root = _pi_sessions_dir()
    if not sessions_root.exists():
        return []

    running_sessions = find_running_pi_sessions()
    sessions: list[ExternalSessionSummary] = []

    # Determine which project directories to scan
    if directory:
        encoded = _encode_directory_name(directory)
        project_dirs = [sessions_root / encoded]
    else:
        project_dirs = [d for d in sessions_root.iterdir() if d.is_dir()]

    for project_dir in project_dirs:
        if not project_dir.exists():
            continue

        for session_file in project_dir.glob("*.jsonl"):
            summary = _parse_session_summary(session_file, running_sessions)
            if summary:
                sessions.append(summary)

    # Sort by last_activity descending
    sessions.sort(key=lambda s: s.last_activity, reverse=True)
    return sessions[:limit]


def _find_session_file(session_id: str) -> Path | None:
    """Find a pi session file by session ID."""
    sessions_root = _pi_sessions_dir()
    if not sessions_root.exists():
        return None

    for project_dir in sessions_root.iterdir():
        if not project_dir.is_dir():
            continue
        for session_file in project_dir.glob("*.jsonl"):
            # Check filename contains the session ID
            if session_id in session_file.stem:
                return session_file

    # Fallback: scan session headers
    for project_dir in sessions_root.iterdir():
        if not project_dir.is_dir():
            continue
        for session_file in project_dir.glob("*.jsonl"):
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        record = json.loads(first_line)
                        if (
                            record.get("type") == "session"
                            and record.get("id") == session_id
                        ):
                            return session_file
            except Exception:
                continue
    return None


def get_pi_session_detail(
    session_id: str,
    limit: int = 100,
) -> ExternalSessionDetail | None:
    """Load full message history for a pi session.

    Args:
        session_id: The session UUID.
        limit: Maximum messages to return.

    Returns:
        Session detail with messages, or None if not found.
    """
    session_file = _find_session_file(session_id)
    if not session_file:
        return None

    running_sessions = find_running_pi_sessions()
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

                record_type = record.get("type")
                timestamp = record.get("timestamp")

                if timestamp:
                    last_activity = timestamp

                if record_type == "session":
                    cwd = record.get("cwd")
                    if cwd:
                        directory = cwd

                if record_type == "message":
                    message = record.get("message", {})
                    role = message.get("role")

                    if role == "user":
                        text = _extract_user_text(message.get("content"))
                        if text:
                            if first_prompt is None:
                                first_prompt = text[:200]
                            last_prompt = text[:200]
                            messages.append(
                                ExternalSessionMessage(
                                    role="user",
                                    content=text,
                                    timestamp=timestamp,
                                )
                            )

                    elif role == "assistant":
                        text, thinking = _extract_assistant_content(
                            message.get("content")
                        )
                        if text or thinking:
                            messages.append(
                                ExternalSessionMessage(
                                    role="assistant",
                                    content=text,
                                    thinking=thinking,
                                    timestamp=timestamp,
                                )
                            )

        # Fallback directory from folder name
        if directory is None:
            directory = _decode_directory_name(session_file.parent.name)

        if last_activity is None:
            mtime = session_file.stat().st_mtime
            from datetime import datetime, timezone

            last_activity = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

        # Apply message limit
        if len(messages) > limit:
            messages = messages[-limit:]

        return ExternalSessionDetail(
            id=session_id,
            runner_type=ExternalRunnerType.PI,
            directory=directory,
            first_prompt=first_prompt,
            last_prompt=last_prompt,
            last_activity=last_activity,
            message_count=len(messages),
            is_running=session_id in running_sessions,
            messages=messages,
        )

    except Exception as e:
        logger.warning(
            "Failed to parse pi session detail",
            session_id=session_id,
            error=str(e),
        )
        return None
