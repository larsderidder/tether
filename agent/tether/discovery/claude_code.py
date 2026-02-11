"""Claude Code session discovery and parsing."""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from tether.discovery.running import find_running_claude_sessions
from tether.models import (
    ExternalRunnerType,
    ExternalSessionSummary,
    ExternalSessionDetail,
    ExternalSessionMessage,
)

logger = structlog.get_logger(__name__)

CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"


def encode_project_path(path: str) -> str:
    """Convert /home/lars/project to -home-lars-project."""
    # Remove leading slash and replace all slashes with dashes
    normalized = path.lstrip("/")
    return "-" + normalized.replace("/", "-")


def decode_project_path(encoded: str) -> str:
    """Convert -home-lars-project to /home/lars/project."""
    # Remove leading dash and replace dashes with slashes
    return "/" + encoded.lstrip("-").replace("-", "/")


_SKIP_PROMPT_PREFIXES = (
    "[Request interrupted",
    "[Response interrupted",
    "[Tool result",
    "<system-",
)


def _extract_user_prompt(content: str | list | None) -> str | None:
    """Extract the user's actual prompt text from message content.

    Skips tool results, system reminders, and interrupted request markers
    that Claude Code stores as user-role messages.
    """
    if isinstance(content, str):
        for prefix in _SKIP_PROMPT_PREFIXES:
            if content.lstrip().startswith(prefix):
                return None
        return content.strip() or None

    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "tool_result":
                    return None  # entire message is a tool result
                text = block.get("text")
                if text:
                    for prefix in _SKIP_PROMPT_PREFIXES:
                        if text.lstrip().startswith(prefix):
                            return None
                    return text.strip() or None
    return None


def _find_session_file(session_id: str) -> Path | None:
    """Find the JSONL file for a session by scanning all projects."""
    if not PROJECTS_DIR.exists():
        return None

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        session_file = project_dir / f"{session_id}.jsonl"
        if session_file.exists():
            return session_file
    return None


def _parse_session_summary(
    session_file: Path,
    running_sessions: set[str],
) -> ExternalSessionSummary | None:
    """Parse a session JSONL file and return summary info."""
    session_id = session_file.stem

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

                # Extract cwd from records (more reliable than folder name)
                if directory is None:
                    cwd = record.get("cwd")
                    if cwd:
                        directory = cwd

                if record_type == "user":
                    message_count += 1
                    # Extract first and last prompt (skip tool results and system messages)
                    message = record.get("message", {})
                    # Skip tool_result records — they are user-role but not prompts
                    if message.get("role") == "user" and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in (message.get("content") or [])
                        if isinstance(message.get("content"), list)
                    ):
                        pass  # skip tool_result messages
                    else:
                        content = message.get("content")
                        text = _extract_user_prompt(content)
                        if text:
                            if first_prompt is None:
                                first_prompt = text[:200]
                            last_prompt = text[:200]

                elif record_type == "assistant":
                    message_count += 1

        # Fallback to decoding folder name if cwd not found in records
        if directory is None:
            project_dir = session_file.parent.name
            directory = decode_project_path(project_dir)

        if last_activity is None:
            # Use file modification time as fallback
            mtime = session_file.stat().st_mtime
            from datetime import datetime, timezone
            last_activity = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

        return ExternalSessionSummary(
            id=session_id,
            runner_type=ExternalRunnerType.CLAUDE_CODE,
            directory=directory,
            first_prompt=first_prompt,
            last_prompt=last_prompt,
            last_activity=last_activity,
            message_count=message_count,
            is_running=session_id in running_sessions,
        )

    except Exception as e:
        logger.warning(
            "Failed to parse Claude session file",
            session_file=str(session_file),
            error=str(e),
        )
        return None


def list_claude_sessions(
    directory: str | None = None,
    limit: int = 50,
) -> list[ExternalSessionSummary]:
    """Discover Claude Code sessions.

    Args:
        directory: Filter to sessions for this project directory.
        limit: Maximum sessions to return.

    Returns:
        List of session summaries sorted by last_activity descending.
    """
    if not PROJECTS_DIR.exists():
        return []

    running_sessions = find_running_claude_sessions()
    sessions: list[ExternalSessionSummary] = []

    # Determine which project directories to scan
    if directory:
        encoded = encode_project_path(directory)
        project_dirs = [PROJECTS_DIR / encoded]
    else:
        project_dirs = [d for d in PROJECTS_DIR.iterdir() if d.is_dir()]

    for project_dir in project_dirs:
        if not project_dir.exists():
            continue

        for session_file in project_dir.glob("*.jsonl"):
            # Skip non-UUID files (like message files)
            name = session_file.stem
            if len(name) < 32 or "-" not in name:
                continue

            summary = _parse_session_summary(session_file, running_sessions)
            if summary:
                sessions.append(summary)

    # Sort by last_activity descending
    sessions.sort(key=lambda s: s.last_activity, reverse=True)
    return sessions[:limit]


def get_claude_session_detail(
    session_id: str,
    limit: int = 100,
) -> ExternalSessionDetail | None:
    """Load full message history for a Claude Code session.

    Args:
        session_id: The session UUID.
        limit: Maximum messages to return.

    Returns:
        Session detail with messages, or None if not found.
    """
    session_file = _find_session_file(session_id)
    if not session_file:
        return None

    running_sessions = find_running_claude_sessions()
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

                # Extract cwd from records
                if directory is None:
                    cwd = record.get("cwd")
                    if cwd:
                        directory = cwd

                if record_type == "user":
                    message = record.get("message", {})
                    content = message.get("content")
                    text, _ = _extract_text_content(content, role="user")
                    if text:
                        candidate = _extract_user_prompt(content)
                        if candidate:
                            if first_prompt is None:
                                first_prompt = candidate[:200]
                            last_prompt = candidate[:200]
                        messages.append(ExternalSessionMessage(
                            role="user",
                            content=text,
                            timestamp=timestamp,
                        ))

                elif record_type == "assistant":
                    message = record.get("message", {})
                    content = message.get("content")
                    text, thinking = _extract_text_content(content, role="assistant")
                    if text or thinking:
                        messages.append(ExternalSessionMessage(
                            role="assistant",
                            content=text,
                            thinking=thinking,
                            timestamp=timestamp,
                        ))

        # Fallback to decoding folder name if cwd not found
        if directory is None:
            project_dir = session_file.parent.name
            directory = decode_project_path(project_dir)

        if last_activity is None:
            mtime = session_file.stat().st_mtime
            from datetime import datetime, timezone
            last_activity = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

        # Apply message limit
        messages = messages[-limit:] if len(messages) > limit else messages

        return ExternalSessionDetail(
            id=session_id,
            runner_type=ExternalRunnerType.CLAUDE_CODE,
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
            "Failed to parse Claude session detail",
            session_id=session_id,
            error=str(e),
        )
        return None


def _extract_text_content(content, role: str = "assistant") -> tuple[str, str | None]:
    """Extract text and thinking from message content.

    For user messages, skip tool_result blocks (those are system-generated).
    For assistant messages, separate text and thinking blocks.

    Returns:
        Tuple of (text_content, thinking_content).
        thinking_content is None for user messages.
    """
    if isinstance(content, str):
        return content, None

    if isinstance(content, list):
        texts = []
        thinking_parts = []
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type", "")

                # Skip tool_result for user messages (they're not actual user input)
                if role == "user" and block_type == "tool_result":
                    continue

                # Handle various block types
                if block_type == "text":
                    text = block.get("text", "")
                    if text:
                        texts.append(text)
                elif block_type == "thinking":
                    # Extract thinking content separately
                    thinking = block.get("thinking", "")
                    if thinking:
                        thinking_parts.append(thinking)
                elif block_type == "tool_use":
                    # Skip tool_use blocks - they're not meaningful text
                    continue
                elif block_type == "tool_result":
                    # Skip tool results - they're verbose and system-generated
                    continue
                elif "text" in block:
                    texts.append(block["text"])

        text_content = "\n".join(texts)
        thinking_content = "\n\n".join(thinking_parts) if thinking_parts else None
        return text_content, thinking_content

    return "", None
