"""Endpoints for discovering external Claude Code and Codex sessions."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Query

from tether.api.deps import require_token
from tether.api.emit import emit_history_message, emit_state
from tether.api.errors import raise_http_error
from tether.api.schemas import (
    AttachSessionRequest,
    ExternalSessionDetailResponse,
    ExternalSessionSummaryResponse,
    SessionResponse,
    SyncResult,
)
from tether.discovery import (
    discover_external_sessions,
    get_external_session_detail,
)
from tether.git import has_git_repository, normalize_directory_path
from tether.session_titles import build_auto_session_name
from tether.models import (
    ExternalRunnerType,
    SessionState,
)
from tether.store import store

router = APIRouter(tags=["external-sessions"])
logger = structlog.get_logger(__name__)


def _format_history_user_text(text: str) -> str:
    """Render imported user prompts clearly inside bridge threads."""
    if "\n" in text:
        return f"👤 User\n{text}"
    return f"👤 User: {text}"


def _get_bound_bridge(session) -> object | None:
    """Return the active bridge for a platform-bound session, if available."""
    platform = session.platform
    if not platform:
        return None

    from tether.bridges.glue import bridge_manager

    bridge = bridge_manager.get_bridge(platform)
    if bridge is None:
        logger.warning(
            "Skipping history relay because bridge is unavailable",
            session_id=session.id,
            platform=platform,
        )
    return bridge


async def _send_history_to_bridge(
    *,
    session,
    bridge: object | None,
    text: str,
    metadata: dict,
) -> None:
    """Send one imported history chunk to the bound bridge thread."""
    if bridge is None or not text.strip():
        return

    try:
        await bridge.on_output(session.id, text, metadata=metadata)
    except Exception:
        logger.exception(
            "Failed to relay external session history to bridge",
            session_id=session.id,
            platform=session.platform,
            metadata=metadata,
        )


async def _relay_history_message_to_bridge(
    *,
    session,
    bridge: object | None,
    role: str,
    content: str,
    thinking: str | None = None,
    is_final: bool = False,
) -> None:
    """Mirror one imported external-session message into the bridge thread."""
    if role == "user":
        await _send_history_to_bridge(
            session=session,
            bridge=bridge,
            text=_format_history_user_text(content),
            metadata={"is_history": True, "role": "user"},
        )
        return

    if thinking:
        await _send_history_to_bridge(
            session=session,
            bridge=bridge,
            text=thinking,
            metadata={
                "is_history": True,
                "role": "assistant",
                "kind": "step",
                "final": False,
            },
        )

    if content:
        kind = "final" if is_final else "step"
        await _send_history_to_bridge(
            session=session,
            bridge=bridge,
            text=content,
            metadata={
                "is_history": True,
                "role": "assistant",
                "kind": kind,
                "final": is_final,
            },
        )


async def _replay_stored_history_to_bridge(*, session, bridge: object | None) -> None:
    """Replay already-emitted imported history into a newly created bridge thread."""
    if bridge is None:
        return

    history_events = [
        event
        for event in store.read_event_log(session.id, since_seq=0)
        if (event.get("data") or {}).get("is_history")
        and event.get("type") in {"user_input", "output"}
    ]

    for event in history_events:
        data = event.get("data") or {}
        if event.get("type") == "user_input":
            await _send_history_to_bridge(
                session=session,
                bridge=bridge,
                text=_format_history_user_text(str(data.get("text") or "")),
                metadata={"is_history": True, "role": "user"},
            )
            continue

        await _send_history_to_bridge(
            session=session,
            bridge=bridge,
            text=str(data.get("text") or ""),
            metadata={
                "is_history": True,
                "role": "assistant",
                "kind": data.get("kind"),
                "final": bool(data.get("final")),
            },
        )


@router.get("/external-sessions", response_model=list[ExternalSessionSummaryResponse])
async def list_external_sessions(
    directory: str | None = Query(None, min_length=1),
    runner_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    _: None = Depends(require_token),
) -> list[ExternalSessionSummaryResponse]:
    """List discoverable external sessions from Claude Code or Codex.

    Args:
        directory: Filter to sessions for this project directory.
        runner_type: Filter to specific runner type ("claude_code" or "codex").
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
                f"Invalid runner_type: {runner_type}. Must be 'claude_code', 'codex', 'opencode', or 'pi'.",
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
    return [
        ExternalSessionSummaryResponse(
            id=s.id,
            runner_type=s.runner_type,
            directory=s.directory,
            first_prompt=s.first_prompt,
            last_prompt=s.last_prompt,
            last_activity=s.last_activity,
            message_count=s.message_count,
            is_running=s.is_running,
        )
        for s in sessions
    ]


@router.get(
    "/external-sessions/{external_id}/history",
    response_model=ExternalSessionDetailResponse,
)
async def get_external_session_history(
    external_id: str,
    runner_type: str = Query(...),
    limit: int = Query(100, ge=1, le=500),
    _: None = Depends(require_token),
) -> ExternalSessionDetailResponse:
    """Get full message history for an external session.

    Args:
        external_id: The external session UUID.
        runner_type: Which runner created the session ("claude_code" or "codex").
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
            f"Invalid runner_type: {runner_type}. Must be 'claude_code', 'codex', 'opencode', or 'pi'.",
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
    return ExternalSessionDetailResponse(
        id=detail.id,
        runner_type=detail.runner_type,
        directory=detail.directory,
        first_prompt=detail.first_prompt,
        last_prompt=detail.last_prompt,
        last_activity=detail.last_activity,
        message_count=detail.message_count,
        is_running=detail.is_running,
        messages=detail.messages,
    )


@router.post("/sessions/attach", response_model=SessionResponse, status_code=201)
async def attach_to_external_session(
    payload: AttachSessionRequest,
    _: None = Depends(require_token),
) -> SessionResponse:
    """Create a Tether session attached to an external session.

    This creates a new Tether session that, when started with input,
    will resume the specified external session instead of starting fresh.

    Body:
        external_id: The external session UUID to attach to.
        runner_type: Which runner created the session ("claude_code" or "codex").
        directory: Working directory for the session.

    Returns:
        New Tether session in AWAITING_INPUT state.
    """
    external_id = payload.external_id
    parsed_runner_type = payload.runner_type
    directory = payload.directory

    logger.info(
        "Attaching to external session",
        external_id=external_id,
        runner_type=parsed_runner_type.value,
        directory=directory,
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
            # Handle platform binding if requested
            if payload.platform and existing_session.platform != payload.platform:
                existing_session.platform = payload.platform
                store.update_session(existing_session)
                try:
                    from tether.bridges.glue import (
                        bridge_manager,
                        make_thread_name,
                        preferred_thread_name_for_platform,
                    )

                    thread_label = preferred_thread_name_for_platform(
                        existing_session,
                        payload.platform,
                    ) or make_thread_name(
                        directory=existing_session.directory or "",
                        runner_type=existing_session.runner_type or "",
                    )
                    thread_info = await bridge_manager.create_thread(
                        existing_session.id,
                        thread_label,
                        platform=payload.platform,
                    )
                    existing_session.platform_thread_id = thread_info.get("thread_id")
                    store.update_session(existing_session)
                    await _replay_stored_history_to_bridge(
                        session=existing_session,
                        bridge=_get_bound_bridge(existing_session),
                    )
                except (ValueError, RuntimeError) as exc:
                    logger.warning("Failed to create platform thread", error=str(exc))
            # Subscribe bridge if platform is bound
            if existing_session.platform:
                from tether.bridges.glue import bridge_subscriber

                bridge_subscriber.subscribe(
                    existing_session.id, existing_session.platform
                )
            # Return the existing session instead of creating a duplicate
            return SessionResponse.from_session(existing_session, store)

    # Verify external session exists and get full history
    detail = get_external_session_detail(
        session_id=external_id,
        runner_type=parsed_runner_type,
        limit=100,  # Get full history to display in session
    )
    if not detail:
        raise_http_error("NOT_FOUND", f"External session not found: {external_id}", 404)

    # Normalize directory
    normalized_directory = normalize_directory_path(directory)

    # Create Tether session
    session = store.create_session(repo_id=normalized_directory, base_ref=None)
    session.repo_display = normalized_directory
    session.directory = normalized_directory
    session.directory_has_git = has_git_repository(normalized_directory)

    # Set runner type based on external session source
    if parsed_runner_type == ExternalRunnerType.CLAUDE_CODE:
        session.runner_type = "claude-local"
    elif parsed_runner_type == ExternalRunnerType.CODEX:
        session.runner_type = "codex"
        session.adapter = "codex_sdk_sidecar"
    elif parsed_runner_type == ExternalRunnerType.OPENCODE:
        session.runner_type = "opencode"
        session.adapter = "opencode"
    elif parsed_runner_type == ExternalRunnerType.PI:
        session.runner_type = "pi"
        session.adapter = "pi_rpc"
    else:
        session.runner_type = "claude-local"  # Default fallback

    # Set session name from first prompt if available
    if detail.first_prompt:
        session.name = build_auto_session_name(session, detail.first_prompt)

    # Pre-register the external session ID for the runner to use
    store.set_runner_session_id(session.id, external_id)
    store.set_workdir(session.id, normalized_directory, managed=False)

    # Platform binding: create messaging thread if requested
    if payload.platform:
        session.platform = payload.platform
        store.update_session(session)
        try:
            from tether.bridges.glue import (
                bridge_manager,
                make_thread_name,
                preferred_thread_name_for_platform,
            )

            thread_label = preferred_thread_name_for_platform(
                session,
                payload.platform,
            ) or make_thread_name(
                directory=normalized_directory, runner_type=session.runner_type
            )
            thread_info = await bridge_manager.create_thread(
                session.id,
                thread_label,
                platform=payload.platform,
            )
            session.platform_thread_id = thread_info.get("thread_id")
        except (ValueError, RuntimeError) as exc:
            store.delete_session(session.id)
            raise_http_error("VALIDATION_ERROR", str(exc), 400)

    # Start in AWAITING_INPUT state (ready to receive input that will resume)
    session.state = SessionState.AWAITING_INPUT
    store.update_session(session)

    # Subscribe bridge if platform is bound
    if session.platform:
        from tether.bridges.glue import bridge_subscriber

        bridge_subscriber.subscribe(session.id, session.platform)
    history_bridge = _get_bound_bridge(session)

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
        await _relay_history_message_to_bridge(
            session=session,
            bridge=history_bridge,
            role=msg.role,
            content=msg.content,
            thinking=msg.thinking,
            is_final=is_final,
        )

    # Track how many messages have been synced (turn_count = user messages only)
    turn_count = sum(1 for m in detail.messages if m.role == "user")
    store.set_synced_message_count(session.id, len(detail.messages), turn_count)

    logger.info(
        "Attached to external session",
        session_id=session.id,
        external_id=external_id,
        history_messages=len(detail.messages),
        turn_count=turn_count,
    )

    return SessionResponse.from_session(session, store)


@router.post("/sessions/{session_id}/sync", response_model=SyncResult)
async def sync_external_session(
    session_id: str,
    _: None = Depends(require_token),
) -> SyncResult:
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

    # Determine external runner type based on session's runner_type
    if session.runner_type == "codex":
        runner_type = ExternalRunnerType.CODEX
    elif session.runner_type == "opencode":
        runner_type = ExternalRunnerType.OPENCODE
    elif session.runner_type == "pi":
        runner_type = ExternalRunnerType.PI
    else:
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

    # If synced_count is 0, the in-memory count was lost (e.g. agent restart).
    # Since this endpoint requires an attached session, messages were already
    # emitted during attach or normal usage. Re-initialize without re-emitting.
    if synced_count == 0:
        turn_count = sum(1 for m in messages if m.role == "user")
        store.set_synced_message_count(session_id, len(messages), turn_count)
        logger.info(
            "Initialized sync count for active session",
            session_id=session_id,
            total_messages=len(messages),
            turn_count=turn_count,
        )
        return SyncResult(synced=0, total=len(messages))

    new_messages = messages[synced_count:]

    if not new_messages:
        logger.info("No new messages to sync", session_id=session_id)
        return SyncResult(synced=0, total=len(messages))

    # Emit new messages
    history_bridge = _get_bound_bridge(session)
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
        await _relay_history_message_to_bridge(
            session=session,
            bridge=history_bridge,
            role=msg.role,
            content=msg.content,
            thinking=msg.thinking,
            is_final=is_final,
        )

    # Update synced count
    turn_count = sum(1 for m in messages if m.role == "user")
    store.set_synced_message_count(session_id, len(messages), turn_count)

    logger.info(
        "Synced external session",
        session_id=session_id,
        new_messages=len(new_messages),
        turn_count=turn_count,
        total_messages=len(messages),
    )

    return SyncResult(synced=len(new_messages), total=len(messages))
