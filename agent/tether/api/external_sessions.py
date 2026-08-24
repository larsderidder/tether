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
from tether.external_session_watcher import external_session_watcher
from tether.external_sync import (
    external_runner_type_for_session,
    format_replay,
    get_bound_bridge,
    get_pi_metadata,
    relay_history_message_to_bridge,
    replay_stored_history_to_bridge,
    sync_external_session_delta,
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

    # Normalize directory if provided. Only resolve to an absolute path when
    # the input already looks absolute; otherwise pass through as-is so
    # providers can do partial/suffix matching on folder names.
    normalized_directory: str | None = None
    if directory:
        normalized_directory = (
            normalize_directory_path(directory)
            if directory.startswith("/") or directory.startswith("~")
            else directory
        )

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
            # Handle platform binding if requested.
            # If the session is already bound to a different platform, refuse
            # unless force=True is set in the payload. This is not exposed yet,
            # so report the existing binding clearly for now.
            if (
                payload.platform
                and existing_session.platform
                and existing_session.platform != payload.platform
            ):
                raise_http_error(
                    "ALREADY_BOUND",
                    f"Session is already bound to {existing_session.platform}. "
                    f"Detach it first or use --bridge {existing_session.platform} to reuse the existing thread.",
                    409,
                )
            if payload.platform and existing_session.approval_mode is None:
                # ASVS 8.2.1 and 8.3.1: remote bridges attach with least privilege.
                existing_session.approval_mode = 0
                store.update_session(existing_session)
            if payload.platform and existing_session.platform != payload.platform:
                existing_session.platform = payload.platform
                store.update_session(existing_session)
                try:
                    from tether.bridges.glue import (
                        bridge_manager,
                        create_or_reuse_thread,
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
                    thread_info = await create_or_reuse_thread(
                        existing_session.id,
                        thread_label,
                        platform=payload.platform,
                        existing_thread_id=existing_session.platform_thread_id,
                    )
                    new_thread_id = thread_info.get("thread_id")
                    existing_session.platform_thread_id = new_thread_id
                    store.update_session(existing_session)
                    actual_runner_type = external_runner_type_for_session(
                        existing_session
                    )
                    detail = get_external_session_detail(
                        session_id=external_id,
                        runner_type=actual_runner_type,
                        limit=100,
                    )
                    if actual_runner_type == ExternalRunnerType.CODEX:
                        await replay_stored_history_to_bridge(
                            session=existing_session,
                            bridge=get_bound_bridge(existing_session),
                        )
                    else:
                        pi_meta = (
                            get_pi_metadata(external_id)
                            if actual_runner_type == ExternalRunnerType.PI
                            else None
                        )
                        replay = format_replay(
                            detail.messages if detail else [], metadata=pi_meta
                        )
                        if replay:
                            await bridge_manager.send_replay(
                                existing_session.id, replay, platform=payload.platform
                            )
                except (ValueError, RuntimeError) as exc:
                    logger.warning("Failed to create platform thread", error=str(exc))
            if not existing_session.external_agent_id:
                existing_session.external_agent_id = external_id
                existing_session.external_agent_type = parsed_runner_type.value
                existing_session.external_agent_name = parsed_runner_type.value
                existing_session.external_agent_workspace = existing_session.directory
                store.update_session(existing_session)

            # Subscribe bridge if platform is bound
            if existing_session.platform:
                from tether.bridges.glue import bridge_subscriber

                bridge_subscriber.subscribe(
                    existing_session.id, existing_session.platform
                )
                external_session_watcher.register(existing_session.id)
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

    session.external_agent_id = external_id
    session.external_agent_type = parsed_runner_type.value
    session.external_agent_name = parsed_runner_type.value
    session.external_agent_workspace = normalized_directory

    # Set session name from first prompt if available
    if detail.first_prompt:
        session.name = build_auto_session_name(session, detail.first_prompt)

    # Pre-register the external session ID for the runner to use
    store.set_runner_session_id(session.id, external_id)
    store.set_workdir(session.id, normalized_directory, managed=False)

    # Platform binding: create messaging thread if requested
    if payload.platform:
        session.platform = payload.platform
        # ASVS 8.2.1 and 8.3.1: remote bridges attach with least privilege.
        session.approval_mode = 0
        store.update_session(session)
        try:
            from tether.bridges.glue import (
                bridge_manager,
                create_or_reuse_thread,
                make_thread_name,
                preferred_thread_name_for_platform,
            )

            thread_label = preferred_thread_name_for_platform(
                session,
                payload.platform,
            ) or make_thread_name(
                directory=normalized_directory, runner_type=session.runner_type
            )
            thread_info = await create_or_reuse_thread(
                session.id,
                thread_label,
                platform=payload.platform,
                existing_thread_id=session.platform_thread_id,
            )
            new_thread_id = thread_info.get("thread_id")
            session.platform_thread_id = new_thread_id

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
    history_bridge = get_bound_bridge(session)

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
            is_history=True,
        )
        await relay_history_message_to_bridge(
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
    if session.platform:
        external_session_watcher.register(session.id)

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
    force: bool = Query(False),
    _: None = Depends(require_token),
) -> SyncResult:
    """Sync new messages from the attached external session."""
    logger.info("Sync requested", session_id=session_id)
    return await sync_external_session_delta(session_id, force=force, source="manual")
