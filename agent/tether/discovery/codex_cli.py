"""Codex CLI session discovery and parsing."""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

import structlog

from tether.discovery.running import find_running_codex_sessions
from tether.models import (
    ExternalRunnerType,
    ExternalSessionSummary,
    ExternalSessionDetail,
    ExternalSessionMessage,
)

logger = structlog.get_logger("tether.discovery.codex_cli")

CODEX_HOME = Path.home() / ".codex"
SESSIONS_DIR = CODEX_HOME / "sessions"


def _parse_session_file(
    session_file: Path,
    running_sessions: set[str],
) -> ExternalSessionSummary | None:
    """Parse a Codex CLI session JSONL file and return summary."""
    session_id: str | None = None
    directory: str | None = None
    first_prompt: str | None = None
    last_activity: str | None = None
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

                if record_type == "session_meta":
                    payload = record.get("payload", {})
                    session_id = payload.get("id")
                    directory = payload.get("cwd")

                elif record_type == "response_item":
                    payload = record.get("payload", {})
                    role = payload.get("role")
                    if role in ("user", "assistant"):
                        message_count += 1
                        if role == "user" and first_prompt is None:
                            content = payload.get("content", [])
                            text = _extract_codex_text(content)
                            if text and not text.startswith("<"):
                                # Skip environment context blocks
                                first_prompt = text[:200]

                elif record_type == "event_msg":
                    payload = record.get("payload", {})
                    if payload.get("type") == "user_message":
                        message = payload.get("message", "")
                        if message and first_prompt is None:
                            first_prompt = message[:200]

        if not session_id or not directory:
            return None

        if last_activity is None:
            mtime = session_file.stat().st_mtime
            last_activity = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

        return ExternalSessionSummary(
            id=session_id,
            runner_type=ExternalRunnerType.CODEX_CLI,
            directory=directory,
            first_prompt=first_prompt,
            last_activity=last_activity,
            message_count=message_count,
            is_running=session_id in running_sessions,
        )

    except Exception as e:
        logger.warning(
            "Failed to parse Codex session file",
            session_file=str(session_file),
            error=str(e),
        )
        return None


def _find_session_file(session_id: str) -> Path | None:
    """Find the JSONL file for a session by scanning date directories."""
    if not SESSIONS_DIR.exists():
        return None

    # Walk through year/month/day directories
    for year_dir in SESSIONS_DIR.iterdir():
        if not year_dir.is_dir():
            continue
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir():
                continue
            for day_dir in month_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                # Look for files containing the session ID
                for session_file in day_dir.glob(f"*{session_id}*.jsonl"):
                    return session_file
    return None


def list_codex_sessions(
    directory: str | None = None,
    limit: int = 50,
) -> list[ExternalSessionSummary]:
    """Discover Codex CLI sessions.

    Args:
        directory: Filter to sessions for this project directory.
        limit: Maximum sessions to return.

    Returns:
        List of session summaries sorted by last_activity descending.
    """
    if not SESSIONS_DIR.exists():
        return []

    running_sessions = find_running_codex_sessions()
    sessions: list[ExternalSessionSummary] = []
    seen_ids: set[str] = set()

    # Walk through year/month/day directories (most recent first)
    year_dirs = sorted(SESSIONS_DIR.iterdir(), reverse=True)
    for year_dir in year_dirs:
        if not year_dir.is_dir():
            continue
        month_dirs = sorted(year_dir.iterdir(), reverse=True)
        for month_dir in month_dirs:
            if not month_dir.is_dir():
                continue
            day_dirs = sorted(month_dir.iterdir(), reverse=True)
            for day_dir in day_dirs:
                if not day_dir.is_dir():
                    continue

                # Parse each session file in this day
                for session_file in day_dir.glob("rollout-*.jsonl"):
                    summary = _parse_session_file(session_file, running_sessions)
                    if summary:
                        # Filter by directory if specified
                        if directory and summary.directory != directory:
                            continue
                        # Avoid duplicates (same session ID)
                        if summary.id in seen_ids:
                            continue
                        seen_ids.add(summary.id)
                        sessions.append(summary)

                        # Early exit if we have enough
                        if len(sessions) >= limit:
                            break

                if len(sessions) >= limit:
                    break
            if len(sessions) >= limit:
                break
        if len(sessions) >= limit:
            break

    # Sort by last_activity descending
    sessions.sort(key=lambda s: s.last_activity, reverse=True)
    return sessions[:limit]


def get_codex_session_detail(
    session_id: str,
    limit: int = 100,
) -> ExternalSessionDetail | None:
    """Load full message history for a Codex CLI session.

    Args:
        session_id: The session UUID.
        limit: Maximum messages to return.

    Returns:
        Session detail with messages, or None if not found.
    """
    session_file = _find_session_file(session_id)
    if not session_file:
        return None

    running_sessions = find_running_codex_sessions()
    directory: str | None = None
    first_prompt: str | None = None
    last_activity: str | None = None
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

                if record_type == "session_meta":
                    payload = record.get("payload", {})
                    directory = payload.get("cwd")

                elif record_type == "response_item":
                    payload = record.get("payload", {})
                    role = payload.get("role")
                    if role == "user":
                        content = payload.get("content", [])
                        text = _extract_codex_text(content)
                        # Skip environment context blocks
                        if text and not text.startswith("<environment_context>"):
                            if first_prompt is None:
                                first_prompt = text[:200]
                            messages.append(ExternalSessionMessage(
                                role="user",
                                content=text,
                                timestamp=timestamp,
                            ))
                    elif role == "assistant":
                        content = payload.get("content", [])
                        text = _extract_codex_text(content)
                        if text:
                            messages.append(ExternalSessionMessage(
                                role="assistant",
                                content=text,
                                timestamp=timestamp,
                            ))

        if not directory:
            return None

        if last_activity is None:
            mtime = session_file.stat().st_mtime
            last_activity = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

        # Apply message limit
        messages = messages[-limit:] if len(messages) > limit else messages

        return ExternalSessionDetail(
            id=session_id,
            runner_type=ExternalRunnerType.CODEX_CLI,
            directory=directory,
            first_prompt=first_prompt,
            last_activity=last_activity,
            message_count=len(messages),
            is_running=session_id in running_sessions,
            messages=messages,
        )

    except Exception as e:
        logger.warning(
            "Failed to parse Codex session detail",
            session_id=session_id,
            error=str(e),
        )
        return None


def _extract_codex_text(content) -> str:
    """Extract text from Codex message content blocks."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type", "")
                if block_type == "input_text":
                    texts.append(block.get("text", ""))
                elif block_type == "output_text":
                    texts.append(block.get("text", ""))
                elif block_type == "text":
                    texts.append(block.get("text", ""))
                elif "text" in block:
                    texts.append(block["text"])
        return "\n".join(texts)

    return ""
