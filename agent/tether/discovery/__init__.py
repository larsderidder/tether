"""Compatibility layer around agent-sessions discovery providers."""

from __future__ import annotations

from agent_sessions import (
    RunnerType as ExternalRunnerType,
    SessionDetail as ExternalSessionDetail,
    SessionMessage as ExternalSessionMessage,
    SessionSummary as ExternalSessionSummary,
    get_claude_session_detail,
    get_opencode_session_detail,
    get_pi_session_detail,
    list_claude_sessions,
    list_opencode_sessions,
    list_pi_sessions,
)

from tether.discovery.codex_sessions import (
    get_codex_session_detail,
    list_codex_sessions,
)


def discover_external_sessions(
    directory: str | None = None,
    runner_type: ExternalRunnerType | None = None,
    limit: int = 50,
) -> list[ExternalSessionSummary]:
    """Discover external sessions using local provider compatibility fixes."""

    sessions: list[ExternalSessionSummary] = []

    if runner_type is None or runner_type == ExternalRunnerType.CLAUDE_CODE:
        sessions.extend(list_claude_sessions(directory=directory, limit=limit))
    if runner_type is None or runner_type == ExternalRunnerType.CODEX:
        sessions.extend(list_codex_sessions(directory=directory, limit=limit))
    if runner_type is None or runner_type == ExternalRunnerType.OPENCODE:
        sessions.extend(list_opencode_sessions(directory=directory, limit=limit))
    if runner_type is None or runner_type == ExternalRunnerType.PI:
        sessions.extend(list_pi_sessions(directory=directory, limit=limit))

    sessions.sort(key=lambda item: item.last_activity, reverse=True)
    return sessions[:limit]


def get_external_session_detail(
    session_id: str,
    runner_type: ExternalRunnerType,
    limit: int = 100,
) -> ExternalSessionDetail | None:
    """Load full session detail using local provider compatibility fixes."""

    if runner_type == ExternalRunnerType.CLAUDE_CODE:
        return get_claude_session_detail(session_id, limit=limit)
    if runner_type == ExternalRunnerType.CODEX:
        return get_codex_session_detail(session_id, limit=limit)
    if runner_type == ExternalRunnerType.OPENCODE:
        return get_opencode_session_detail(session_id, limit=limit)
    if runner_type == ExternalRunnerType.PI:
        return get_pi_session_detail(session_id, limit=limit)
    return None


__all__ = [
    "discover_external_sessions",
    "get_external_session_detail",
    "ExternalRunnerType",
    "ExternalSessionSummary",
    "ExternalSessionDetail",
    "ExternalSessionMessage",
    "list_codex_sessions",
    "get_codex_session_detail",
]
