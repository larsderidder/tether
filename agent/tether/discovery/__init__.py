"""External session discovery for Claude Code and Codex."""

from __future__ import annotations

from tether.discovery.claude_code import (
    list_claude_sessions,
    get_claude_session_detail,
)
from tether.discovery.codex_sessions import (
    list_codex_sessions,
    get_codex_session_detail,
)
from tether.models import (
    ExternalRunnerType,
    ExternalSessionSummary,
    ExternalSessionDetail,
)


def discover_external_sessions(
    directory: str | None = None,
    runner_type: ExternalRunnerType | None = None,
    limit: int = 50,
) -> list[ExternalSessionSummary]:
    """Discover external sessions from Claude Code.

    Args:
        directory: Filter to sessions for this project directory.
        runner_type: Filter to specific runner type.
        limit: Maximum sessions to return per runner type.

    Returns:
        List of session summaries, sorted by last_activity descending.
    """
    sessions: list[ExternalSessionSummary] = []

    if runner_type is None or runner_type == ExternalRunnerType.CLAUDE_CODE:
        sessions.extend(list_claude_sessions(directory=directory, limit=limit))
    if runner_type is None or runner_type == ExternalRunnerType.CODEX:
        sessions.extend(list_codex_sessions(directory=directory, limit=limit))

    # Sort by last_activity descending (most recent first)
    sessions.sort(key=lambda s: s.last_activity, reverse=True)

    # Apply overall limit
    return sessions[:limit]


def get_external_session_detail(
    session_id: str,
    runner_type: ExternalRunnerType,
    limit: int = 100,
) -> ExternalSessionDetail | None:
    """Load full session detail with message history.

    Args:
        session_id: The external session UUID.
        runner_type: Which runner created the session.
        limit: Maximum messages to return.

    Returns:
        Session detail with messages, or None if not found.
    """
    if runner_type == ExternalRunnerType.CLAUDE_CODE:
        return get_claude_session_detail(session_id, limit=limit)
    if runner_type == ExternalRunnerType.CODEX:
        return get_codex_session_detail(session_id, limit=limit)
    return None


__all__ = [
    "discover_external_sessions",
    "get_external_session_detail",
    "ExternalRunnerType",
    "ExternalSessionSummary",
    "ExternalSessionDetail",
]
