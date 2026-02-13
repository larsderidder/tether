"""Compatibility shim: re-exports from agent_sessions.

Maps Tether's External* naming to agent_sessions' naming convention.
"""
# ruff: noqa: F401

from agent_sessions import (
    RunnerType as ExternalRunnerType,
    SessionSummary as ExternalSessionSummary,
    SessionDetail as ExternalSessionDetail,
    SessionMessage as ExternalSessionMessage,
    discover_sessions as discover_external_sessions,
    get_session_detail as get_external_session_detail,
    list_claude_sessions,
    get_claude_session_detail,
    list_codex_sessions,
    get_codex_session_detail,
    list_pi_sessions,
    get_pi_session_detail,
)

__all__ = [
    "discover_external_sessions",
    "get_external_session_detail",
    "ExternalRunnerType",
    "ExternalSessionSummary",
    "ExternalSessionDetail",
    "ExternalSessionMessage",
]
