"""Endpoints for discovering external Claude Code and Codex CLI sessions."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Query, Body

from tether.api.deps import require_token
from tether.api.emit import emit_state, emit_history_message
from tether.api.errors import raise_http_error
from tether.discovery import (
    discover_external_sessions,
    get_external_session_detail,
)
from tether.git import has_git_repository, normalize_directory_path
from tether.models import (
    ExternalRunnerType,
    ExternalSessionSummary,
    ExternalSessionDetail,
    SessionState,
)
from tether.store import store

router = APIRouter(tags=["external-sessions"])
logger = structlog.get_logger("tether.api.external_sessions")


@router.get("/external-sessions", response_model=dict)
async def list_external_sessions(
    directory: str | None = Query(None, min_length=1),
    runner_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    _: None = Depends(require_token),
) -> dict:
    """List discoverable external sessions from Claude Code and Codex CLI.

    Args:
        directory: Filter to sessions for this project directory.
        runner_type: Filter to specific runner type ("claude_code" or "codex_cli").
        limit: Maximum sessions to return.

    Returns:
        List of session summaries with id, directory, first_prompt, etc.
    """
    logger.info(
        "Listing external sessions",
        directory=directory,
        runner_type=runner_type,
        limit=limit,
    )

    # Parse runner_type filter
    parsed_runner_type: ExternalRunnerType | None = None
    if runner_type:
        try:
            parsed_runner_type = ExternalRunnerType(runner_type)
        except ValueError:
            raise_http_error(
                "VALIDATION_ERROR",
                f"Invalid runner_type: {runner_type}. Must be 'claude_code' or 'codex_cli'.",
                422,
            )

    # Normalize directory if provided
    normalized_directory: str | None = None
    if directory:
        normalized_directory = normalize_directory_path(directory)

    sessions = discover_external_sessions(
        directory=normalized_directory,
        runner_type=parsed_runner_type,
        limit=limit,
    )

    logger.info("Found external sessions", count=len(sessions))
    return {
        "sessions": [_serialize_external_summary(s) for s in sessions],
    }


@router.get("/external-sessions/{external_id}/history", response_model=dict)
async def get_external_session_history(
    external_id: str,
    runner_type: str = Query(...),
    limit: int = Query(100, ge=1, le=500),
    _: None = Depends(require_token),
) -> dict:
    """Get full message history for an external session.

    Args:
        external_id: The external session UUID.
        runner_type: Which runner created the session ("claude_code" or "codex_cli").
        limit: Maximum messages to return.

    Returns:
        Session detail with full message history.
    """
    logger.info(
        "Fetching external session history",
        external_id=external_id,
        runner_type=runner_type,
        limit=limit,
    )

    # Parse runner_type
    try:
        parsed_runner_type = ExternalRunnerType(runner_type)
    except ValueError:
        raise_http_error(
            "VALIDATION_ERROR",
            f"Invalid runner_type: {runner_type}. Must be 'claude_code' or 'codex_cli'.",
            422,
        )

    detail = get_external_session_detail(
        session_id=external_id,
        runner_type=parsed_runner_type,
        limit=limit,
    )

    if not detail:
        raise_http_error("NOT_FOUND", f"External session not found: {external_id}", 404)

    logger.info(
        "Fetched external session history",
        external_id=external_id,
        message_count=len(detail.messages),
    )
    return {"session": _serialize_external_detail(detail)}


@router.post("/sessions/attach", response_model=dict, status_code=201)
async def attach_to_external_session(
    payload: dict = Body(...),
    _: None = Depends(require_token),
) -> dict:
    """Create a Tether session attached to an external session.

    This creates a new Tether session that, when started with input,
    will resume the specified external session instead of starting fresh.

    Body:
        external_id: The external session UUID to attach to.
        runner_type: Which runner created the session ("claude_code").
        directory: Working directory for the session.

    Returns:
        New Tether session in AWAITING_INPUT state.

    Note:
        Only Claude Code sessions support attachment (resume).
        Codex CLI sessions are view-only.
    """
    external_id = payload.get("external_id")
    runner_type = payload.get("runner_type")
    directory = payload.get("directory")

    if not external_id:
        raise_http_error("VALIDATION_ERROR", "external_id is required", 422)
    if not runner_type:
        raise_http_error("VALIDATION_ERROR", "runner_type is required", 422)
    if not directory:
        raise_http_error("VALIDATION_ERROR", "directory is required", 422)

    logger.info(
        "Attaching to external session",
        external_id=external_id,
        runner_type=runner_type,
        directory=directory,
    )

    # Parse and validate runner_type
    try:
        parsed_runner_type = ExternalRunnerType(runner_type)
    except ValueError:
        raise_http_error(
            "VALIDATION_ERROR",
            f"Invalid runner_type: {runner_type}. Must be 'claude_code' or 'codex_cli'.",
            422,
        )

    # Only Claude Code supports attachment
    if parsed_runner_type != ExternalRunnerType.CLAUDE_CODE:
        raise_http_error(
            "RUNNER_NOT_SUPPORTED",
            "Only Claude Code sessions support attachment. Codex CLI sessions are view-only.",
            422,
        )

    # Check if this external session is already attached to a Tether session
    existing_session_id = store.find_session_by_runner_session_id(external_id)
    if existing_session_id:
        existing_session = store.get_session(existing_session_id)
        if existing_session:
            logger.info(
                "External session already attached",
                external_id=external_id,
                existing_session_id=existing_session_id,
            )
            # Return the existing session instead of creating a duplicate
            return {
                "session": {
                    "id": existing_session.id,
                    "state": existing_session.state.value,
                    "name": existing_session.name,
                    "created_at": existing_session.created_at,
                    "started_at": existing_session.started_at,
                    "ended_at": existing_session.ended_at,
                    "last_activity_at": existing_session.last_activity_at,
                    "exit_code": existing_session.exit_code,
                    "summary": existing_session.summary,
                    "runner_header": existing_session.runner_header,
                    "runner_type": existing_session.runner_type,
                    "directory": existing_session.directory,
                    "directory_has_git": existing_session.directory_has_git,
                },
            }

    # Verify external session exists and get full history
    detail = get_external_session_detail(
        session_id=external_id,
        runner_type=parsed_runner_type,
        limit=100,  # Get full history to display in session
    )
    if not detail:
        raise_http_error("NOT_FOUND", f"External session not found: {external_id}", 404)

    # Cannot attach to currently running session
    if detail.is_running:
        raise_http_error(
            "INVALID_STATE",
            "Cannot attach to a currently running session. Wait for it to finish or close it first.",
            409,
        )

    # Normalize directory
    normalized_directory = normalize_directory_path(directory)

    # Create Tether session
    session = store.create_session(repo_id=normalized_directory, base_ref=None)
    session.repo_display = normalized_directory
    session.directory = normalized_directory
    session.directory_has_git = has_git_repository(normalized_directory)
    session.runner_type = "claude-local"  # Force claude-local runner for attached sessions

    # Set session name from first prompt if available
    if detail.first_prompt:
        session.name = detail.first_prompt[:80]

    # Pre-register the external session ID for the runner to use
    store.set_runner_session_id(session.id, external_id)
    store.set_workdir(session.id, normalized_directory, managed=False)

    # Start in AWAITING_INPUT state (ready to receive input that will resume)
    session.state = SessionState.AWAITING_INPUT
    store.update_session(session)

    await emit_state(session)

    # Emit the external session's history messages so they appear in the session view
    # For each turn, the last assistant message should be marked as final
    # A turn ends when the next message is from user, or at end of messages
    messages = detail.messages
    for i, msg in enumerate(messages):
        is_final = False
        if msg.role == "assistant":
            # Check if this is the last assistant message before a user message or end
            next_idx = i + 1
            if next_idx >= len(messages) or messages[next_idx].role == "user":
                is_final = True

        await emit_history_message(
            session,
            role=msg.role,
            content=msg.content,
            thinking=msg.thinking,
            timestamp=msg.timestamp,
            is_final=is_final,
        )

    # Track how many messages have been synced
    store.set_synced_message_count(session.id, len(detail.messages))

    logger.info(
        "Attached to external session",
        session_id=session.id,
        external_id=external_id,
        history_messages=len(detail.messages),
    )

    return {
        "session": {
            "id": session.id,
            "state": session.state.value,
            "name": session.name,
            "created_at": session.created_at,
            "started_at": session.started_at,
            "ended_at": session.ended_at,
            "last_activity_at": session.last_activity_at,
            "exit_code": session.exit_code,
            "summary": session.summary,
            "runner_header": session.runner_header,
            "runner_type": session.runner_type,
            "directory": session.directory,
            "directory_has_git": session.directory_has_git,
        },
    }


@router.post("/sessions/{session_id}/sync", response_model=dict)
async def sync_external_session(
    session_id: str,
    _: None = Depends(require_token),
) -> dict:
    """Sync new messages from the attached external session.

    This fetches the latest messages from the external Claude Code session
    and emits any new messages that haven't been synced yet.

    Returns:
        Count of new messages synced.
    """
    logger.info("Sync requested", session_id=session_id)

    session = store.get_session(session_id)
    if not session:
        raise_http_error("NOT_FOUND", "Session not found", 404)

    # Get the external session ID
    external_id = store.get_runner_session_id(session_id)
    if not external_id:
        raise_http_error(
            "INVALID_STATE",
            "Session is not attached to an external session",
            400,
        )

    # Currently only Claude Code supports this
    runner_type = ExternalRunnerType.CLAUDE_CODE

    # Fetch fresh history
    detail = get_external_session_detail(
        session_id=external_id,
        runner_type=runner_type,
        limit=500,
    )
    if not detail:
        raise_http_error("NOT_FOUND", f"External session not found: {external_id}", 404)

    # Get previously synced count
    synced_count = store.get_synced_message_count(session_id)
    messages = detail.messages

    # If synced_count is 0 but session has been used (started_at is set),
    # it means this is a non-attached session that was used normally.
    # Don't emit duplicate messages - just set the count to current total.
    if synced_count == 0 and session.started_at is not None:
        store.set_synced_message_count(session_id, len(messages))
        logger.info(
            "Initialized sync count for active session",
            session_id=session_id,
            total_messages=len(messages),
        )
        return {"synced": 0, "total": len(messages)}

    new_messages = messages[synced_count:]

    if not new_messages:
        logger.info("No new messages to sync", session_id=session_id)
        return {"synced": 0, "total": len(messages)}

    # Emit new messages
    for i, msg in enumerate(new_messages):
        is_final = False
        if msg.role == "assistant":
            # Check if this is the last assistant message before a user message or end
            next_idx = synced_count + i + 1
            if next_idx >= len(messages) or messages[next_idx].role == "user":
                is_final = True

        await emit_history_message(
            session,
            role=msg.role,
            content=msg.content,
            thinking=msg.thinking,
            timestamp=msg.timestamp,
            is_final=is_final,
        )

    # Update synced count
    store.set_synced_message_count(session_id, len(messages))

    logger.info(
        "Synced external session",
        session_id=session_id,
        new_messages=len(new_messages),
        total_messages=len(messages),
    )

    return {"synced": len(new_messages), "total": len(messages)}


def _serialize_external_summary(session: ExternalSessionSummary) -> dict:
    """Serialize an external session summary for API response."""
    return {
        "id": session.id,
        "runner_type": session.runner_type.value,
        "directory": session.directory,
        "first_prompt": session.first_prompt,
        "last_activity": session.last_activity,
        "message_count": session.message_count,
        "is_running": session.is_running,
    }


def _serialize_external_detail(session: ExternalSessionDetail) -> dict:
    """Serialize an external session detail for API response."""
    return {
        "id": session.id,
        "runner_type": session.runner_type.value,
        "directory": session.directory,
        "first_prompt": session.first_prompt,
        "last_activity": session.last_activity,
        "message_count": session.message_count,
        "is_running": session.is_running,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "thinking": m.thinking,
                "timestamp": m.timestamp,
            }
            for m in session.messages
        ],
    }
