"""Session lifecycle endpoints."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends

from tether.api.deps import require_token
from tether.api.diff import build_git_diff
from tether.api.emit import (
    emit_error,
    emit_output,
    emit_permission_request,
    emit_state,
    emit_user_input,
    emit_warning,
)
from tether.api.errors import raise_http_error
from tether.api.runner_events import get_api_runner, get_runner_registry
from tether.api.schemas import (
    AgentEventRequest,
    CreateSessionRequest,
    DiffResponse,
    InputRequest,
    OkResponse,
    PermissionResponseRequest,
    RenameSessionRequest,
    SessionResponse,
    StartSessionRequest,
    UpdateApprovalModeRequest,
)
from tether.api.state import maybe_set_session_name, now, remove_session_lock, session_lock, transition
from tether.bridges.glue import bridge_manager
from tether.diff import parse_git_diff
from tether.discovery.running import is_claude_session_running
from tether.git import has_git_repository, normalize_directory_path
from tether.models import SessionState
from tether.runner import get_runner_type
from tether.runner.base import RunnerUnavailableError
from tether.store import store

router = APIRouter(tags=["sessions"])
logger = structlog.get_logger(__name__)


@contextmanager
def _session_logging_context(session_id: str):
    structlog.contextvars.bind_contextvars(session_id=session_id)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars("session_id")


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(_: None = Depends(require_token)) -> list[SessionResponse]:
    """List all sessions in memory."""
    sessions = store.list_sessions()
    logger.info("Listed sessions", count=len(sessions))
    return [SessionResponse.from_session(session, store) for session in sessions]


@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def create_session(
    payload: CreateSessionRequest,
    _: None = Depends(require_token),
) -> SessionResponse:
    """Create a new session in CREATED state."""
    logger.info(
        "Create session requested",
        repo_id=payload.repo_id,
        directory=payload.directory,
        base_ref=payload.base_ref,
        agent_name=payload.agent_name,
        platform=payload.platform,
    )
    normalized_directory: str | None = None
    if payload.directory:
        candidate = Path(payload.directory).expanduser()
        if not candidate.is_dir():
            raise_http_error("VALIDATION_ERROR", "directory must be an existing folder", 422)
        normalized_directory = normalize_directory_path(payload.directory)
    # Default repo_id to "external" for external agents
    if payload.agent_name and not payload.repo_id and not normalized_directory:
        resolved_repo_id = "external"
    else:
        resolved_repo_id = payload.repo_id or normalized_directory or "repo_local"
    session = store.create_session(repo_id=resolved_repo_id, base_ref=payload.base_ref)

    # Validate and store adapter selection
    if payload.adapter:
        try:
            # Validate adapter early - this will create runner if needed
            get_runner_registry().validate_adapter(payload.adapter)
            session.adapter = payload.adapter
            logger.info("Session adapter configured", adapter=payload.adapter)
        except ValueError as e:
            # Clean up session before returning error
            store.delete_session(session.id)
            raise_http_error(
                "VALIDATION_ERROR",
                f"Invalid adapter '{payload.adapter}': {e}",
                422
            )

    # Populate external agent metadata if provided
    if payload.agent_name:
        import uuid as _uuid

        session.external_agent_id = f"agent_{_uuid.uuid4().hex[:8]}"
        session.external_agent_name = payload.agent_name
        session.external_agent_type = payload.agent_type
        session.external_agent_icon = payload.agent_icon
        session.external_agent_workspace = payload.agent_workspace
    if payload.session_name:
        session.name = payload.session_name

    # Platform binding: create messaging thread
    if payload.platform:
        session.platform = payload.platform
        store.update_session(session)
        try:
            thread_info = await bridge_manager.create_thread(
                session.id, session.name or "New session", platform=payload.platform
            )
            session.platform_thread_id = thread_info.get("thread_id")
        except (ValueError, RuntimeError) as e:
            store.delete_session(session.id)
            raise_http_error("VALIDATION_ERROR", str(e), 400)

    if normalized_directory:
        session.repo_display = normalized_directory
        store.update_session(session)
        store.set_workdir(session.id, normalized_directory, managed=False)
    else:
        store.update_session(session)
    session = store.get_session(session.id) or session

    # Subscribe bridge if platform is bound
    if session.platform:
        from tether.bridges.glue import bridge_subscriber

        bridge_subscriber.subscribe(session.id, session.platform)

    with _session_logging_context(session.id):
        logger.info(
            "Session created",
            repo_id=session.repo_id,
            directory=normalized_directory,
            adapter=session.adapter,
            agent_name=session.external_agent_name,
            platform=session.platform,
        )
        return SessionResponse.from_session(session, store)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, _: None = Depends(require_token)) -> SessionResponse:
    """Fetch a single session by id."""
    with _session_logging_context(session_id):
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)
        logger.info("Fetched session", state=session.state)
        return SessionResponse.from_session(session, store)


@router.delete("/sessions/{session_id}", response_model=OkResponse)
async def delete_session(session_id: str, _: None = Depends(require_token)) -> OkResponse:
    """Delete a session if it is not running."""
    with _session_logging_context(session_id):
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)
        if session.state in (SessionState.RUNNING, SessionState.INTERRUPTING):
            raise_http_error("INVALID_STATE", "Session is active", 409)

        # Clean up pending permission futures before deleting
        store.clear_pending_permissions(session_id)

        # Cancel bridge subscriber if one is running
        if session.platform:
            from tether.bridges.glue import bridge_subscriber

            await bridge_subscriber.unsubscribe(session_id, platform=session.platform)

        store.delete_session(session_id)
        remove_session_lock(session_id)
        logger.info("Session deleted")
        return OkResponse()


@router.get("/sessions/{session_id}/usage")
async def session_usage(session_id: str, _: None = Depends(require_token)) -> dict:
    """Get aggregated token and cost usage for a session."""
    session = store.get_session(session_id)
    if not session:
        raise_http_error("NOT_FOUND", "Session not found", 404)
    return store.session_usage(session_id)


@router.post("/sessions/{session_id}/start", response_model=SessionResponse)
async def start_session(
    session_id: str,
    payload: StartSessionRequest,
    _: None = Depends(require_token),
) -> SessionResponse:
    """Start a session and launch the configured runner."""
    with _session_logging_context(session_id):
        # Phase 1: validate and transition to RUNNING under lock
        async with session_lock(session_id):
            session = store.get_session(session_id)
            if not session:
                raise_http_error("NOT_FOUND", "Session not found", 404)
            if session.state not in (SessionState.CREATED, SessionState.AWAITING_INPUT, SessionState.ERROR):
                raise_http_error("INVALID_STATE", "Session not ready to start", 409)

            # Validate inputs BEFORE state transition
            if not session.directory:
                raise_http_error("VALIDATION_ERROR", "Session has no directory assigned", 422)
            prompt = payload.prompt
            approval_choice = payload.approval_choice

            # Persist approval mode so the runner can recover it after restart
            session.approval_mode = approval_choice
            store.update_session(session)

            # Clear stale state from previous runs
            # NOTE: Do NOT clear runner_session_id here - it may contain an attached
            # external session ID that we need to preserve for resume
            if session.state in (SessionState.AWAITING_INPUT, SessionState.ERROR):
                session.ended_at = None
                session.exit_code = None
                session.summary = None
                if hasattr(session, "runner_header"):
                    session.runner_header = None
                store.clear_process(session_id)
                store.clear_pending_inputs(session_id)
                store.clear_last_output(session_id)

            logger.info("Session start requested")

            # Warn if the attached external session is currently busy in another CLI
            external_session_id = store.get_runner_session_id(session_id)
            if external_session_id and is_claude_session_running(external_session_id):
                logger.warning(
                    "External session is busy at start",
                    session_id=session_id,
                    external_session_id=external_session_id,
                )
                await emit_warning(
                    session,
                    "EXTERNAL_SESSION_BUSY",
                    "The attached Claude session is currently running in another CLI. "
                    "Your message will be sent, but may not appear in the other CLI until it's restarted.",
                )

            # Get runner for this session's adapter
            runner = get_api_runner(session.adapter)
            session.runner_type = runner.runner_type
            adapter = session.adapter

            if not store.get_workdir(session_id):
                store.set_workdir(session_id, session.directory, managed=False)
            maybe_set_session_name(session, prompt)

            # Transition to RUNNING and attempt to start the runner
            transition(session, SessionState.RUNNING, started_at=True)
            await emit_state(session)

            if prompt:
                await emit_user_input(session, prompt)

        # Phase 2: launch the runner with lock released.
        # Runner callbacks (on_error, on_exit, on_awaiting_input) acquire
        # the session lock, so we must not hold it here.
        start_error: tuple[str, Exception] | None = None
        try:
            await runner.start(session_id, prompt, approval_choice)
            logger.info(
                "Session started",
                adapter=adapter or "default",
                runner_type=runner.runner_type,
            )
        except RunnerUnavailableError as exc:
            start_error = ("unavailable", exc)
        except Exception as exc:
            start_error = ("error", exc)

        # Phase 3: handle errors under lock
        if start_error:
            async with session_lock(session_id):
                session = store.get_session(session_id)
                if not session:
                    raise_http_error("NOT_FOUND", "Session not found", 404)
                kind, exc = start_error
                if session.state != SessionState.ERROR:
                    transition(session, SessionState.ERROR, ended_at=True)
                    await emit_state(session)
                if kind == "unavailable":
                    logger.warning(
                        "Runner unavailable during start",
                        session_id=session_id,
                        error=str(exc),
                    )
                    if (adapter or "").lower() == "codex_sdk_sidecar":
                        raise_http_error(
                            "AGENT_UNAVAILABLE",
                            "Codex sidecar is not reachable. Start `codex-sdk-sidecar` and try again.",
                            503,
                        )
                    raise_http_error(
                        "AGENT_UNAVAILABLE",
                        "Runner backend is not reachable. Check that the adapter is running and try again.",
                        503,
                    )
                else:
                    logger.exception("Runner failed to start", session_id=session_id)
                    raise_http_error("RUNNER_ERROR", f"Failed to start runner: {exc}", 500)

        session = store.get_session(session_id)
        return SessionResponse.from_session(session, store)


@router.patch("/sessions/{session_id}/rename", response_model=SessionResponse)
async def rename_session(
    session_id: str,
    payload: RenameSessionRequest,
    _: None = Depends(require_token),
) -> SessionResponse:
    """Rename an existing session."""
    with _session_logging_context(session_id):
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)
        # Clean up whitespace (validation already ensures non-empty and max 80 chars)
        cleaned = " ".join(payload.name.split())
        session.name = cleaned
        store.update_session(session)
        logger.info("Session renamed", name=session.name)
        return SessionResponse.from_session(session, store)


@router.post("/sessions/{session_id}/input", response_model=SessionResponse)
async def send_input(
    session_id: str,
    payload: InputRequest,
    _: None = Depends(require_token),
) -> SessionResponse:
    """Send input to a running or awaiting session."""
    with _session_logging_context(session_id):
        # Phase 1: validate and transition under lock
        async with session_lock(session_id):
            text = payload.text
            session = store.get_session(session_id)
            if not session:
                raise_http_error("NOT_FOUND", "Session not found", 404)
            if session.state not in (
                SessionState.RUNNING,
                SessionState.AWAITING_INPUT,
                SessionState.ERROR,
            ):
                raise_http_error(
                    "INVALID_STATE",
                    "Session not running, awaiting input, or recoverable error",
                    409,
                )
            logger.info("Session input received", text_length=len(text))

            # Warn if the attached external session is currently busy in another CLI
            external_session_id = store.get_runner_session_id(session_id)
            if external_session_id and is_claude_session_running(external_session_id):
                logger.warning(
                    "External session is busy",
                    session_id=session_id,
                    external_session_id=external_session_id,
                )
                await emit_warning(
                    session,
                    "EXTERNAL_SESSION_BUSY",
                    "The attached Claude session is currently running in another CLI. "
                    "Your message will be sent, but may not appear in the other CLI until it's restarted.",
                )

            # Clear stale error state so the session can resume
            if session.state == SessionState.ERROR:
                session.ended_at = None
                session.exit_code = None
                session.summary = None
                if hasattr(session, "runner_header"):
                    session.runner_header = None
                store.clear_process(session_id)
                store.clear_pending_inputs(session_id)
                store.clear_last_output(session_id)

            # Transition to RUNNING when user provides input
            if session.state in (SessionState.AWAITING_INPUT, SessionState.ERROR):
                transition(session, SessionState.RUNNING)
                await emit_state(session)
            maybe_set_session_name(session, text)
            await emit_user_input(session, text)
            adapter = session.adapter

        # Phase 2: forward to runner with lock released.
        # Runner callbacks (on_error, on_exit, on_awaiting_input) acquire
        # the session lock, so we must not hold it here.
        runner = get_api_runner(adapter)
        send_error: tuple[str, Exception] | None = None
        try:
            await runner.send_input(session_id, text)
        except RunnerUnavailableError as exc:
            send_error = ("unavailable", exc)
        except Exception as exc:
            send_error = ("error", exc)

        # Phase 3: finalize under lock
        async with session_lock(session_id):
            session = store.get_session(session_id)
            if not session:
                raise_http_error("NOT_FOUND", "Session not found", 404)

            if send_error:
                kind, exc = send_error
                if session.state != SessionState.ERROR:
                    transition(session, SessionState.ERROR, ended_at=True)
                    await emit_state(session)
                if kind == "unavailable":
                    logger.warning(
                        "Runner unavailable during input",
                        session_id=session_id,
                        error=str(exc),
                    )
                    if (adapter or "").lower() == "codex_sdk_sidecar":
                        raise_http_error(
                            "AGENT_UNAVAILABLE",
                            "Codex sidecar is not reachable. Start `codex-sdk-sidecar` and try again.",
                            503,
                        )
                    raise_http_error(
                        "AGENT_UNAVAILABLE",
                        "Runner backend is not reachable. Check that the adapter is running and try again.",
                        503,
                    )
                else:
                    logger.exception("Runner failed while sending input", session_id=session_id)
                    raise_http_error("RUNNER_ERROR", f"Failed to send input: {exc}", 500)

            session.last_activity_at = now()
            store.update_session(session)
            logger.info("Session input forwarded")
            return SessionResponse.from_session(session, store)


@router.post("/sessions/{session_id}/interrupt", response_model=SessionResponse)
async def interrupt_session(session_id: str, _: None = Depends(require_token)) -> SessionResponse:
    """Interrupt the current turn. Session remains active and can continue with new input."""
    with _session_logging_context(session_id):
        # Phase 1: validate and transition to INTERRUPTING under lock
        async with session_lock(session_id):
            session = store.get_session(session_id)
            if not session:
                raise_http_error("NOT_FOUND", "Session not found", 404)
            # Idempotent: already awaiting input or interrupting
            if session.state in (SessionState.AWAITING_INPUT, SessionState.INTERRUPTING):
                logger.info("Session interrupt requested but already idle/interrupting")
                return SessionResponse.from_session(session, store)
            if session.state in (SessionState.CREATED, SessionState.ERROR):
                raise_http_error("INVALID_STATE", "Session not running", 409)
            # Transition to INTERRUPTING
            transition(session, SessionState.INTERRUPTING)
            await emit_state(session)
            logger.info("Interrupting session")
            adapter = session.adapter

        # Phase 2: stop the runner with lock released.
        # runner.stop() awaits the query task, whose finally-block callbacks
        # (on_exit, on_awaiting_input) acquire the session lock.  Holding the
        # lock here would deadlock.
        runner = get_api_runner(adapter)
        stop_error: tuple[str, Exception] | None = None
        try:
            await runner.stop(session_id)
        except RunnerUnavailableError as exc:
            stop_error = ("unavailable", exc)
        except Exception as exc:
            stop_error = ("error", exc)

        # Phase 3: finalize under lock
        async with session_lock(session_id):
            session = store.get_session(session_id)
            if not session:
                raise_http_error("NOT_FOUND", "Session not found", 404)

            if stop_error:
                kind, exc = stop_error
                # Only transition if a callback hasn't already moved to ERROR
                if session.state != SessionState.ERROR:
                    transition(session, SessionState.ERROR, ended_at=True)
                    await emit_state(session)
                if kind == "unavailable":
                    logger.warning(
                        "Runner unavailable during interrupt",
                        session_id=session_id,
                        error=str(exc),
                    )
                    if (adapter or "").lower() == "codex_sdk_sidecar":
                        raise_http_error(
                            "AGENT_UNAVAILABLE",
                            "Codex sidecar is not reachable. Start `codex-sdk-sidecar` and try again.",
                            503,
                        )
                    raise_http_error(
                        "AGENT_UNAVAILABLE",
                        "Runner backend is not reachable. Check that the adapter is running and try again.",
                        503,
                    )
                else:
                    logger.exception("Runner failed while interrupting", session_id=session_id)
                    raise_http_error("RUNNER_ERROR", f"Failed to interrupt session: {exc}", 500)

            # Transition to AWAITING_INPUT after interrupt completes
            if session.state == SessionState.INTERRUPTING:
                transition(session, SessionState.AWAITING_INPUT)
                await emit_state(session)
            logger.info("Session interrupted")
            return SessionResponse.from_session(session, store)


@router.get("/sessions/{session_id}/diff", response_model=DiffResponse)
async def get_diff(session_id: str, _: None = Depends(require_token)) -> DiffResponse:
    """Return the git diff for the session's working directory."""
    with _session_logging_context(session_id):
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)
        logger.info("Session diff requested")
        target = session.directory or store.get_workdir(session_id)
        if target and Path(target).is_dir() and has_git_repository(target):
            diff_text = build_git_diff(target)
            files = parse_git_diff(diff_text)
            return DiffResponse(diff=diff_text, files=files)
        logger.info("Diff unavailable", path=target, reason="no repository")
        return DiffResponse(diff="", files=[])


@router.patch("/sessions/{session_id}/approval-mode", response_model=SessionResponse)
async def update_approval_mode(
    session_id: str,
    payload: UpdateApprovalModeRequest,
    _: None = Depends(require_token),
) -> SessionResponse:
    """Update the approval mode for a session."""
    with _session_logging_context(session_id):
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)

        session.approval_mode = payload.approval_mode
        store.update_session(session)

        # Update runner's permission mode if session is active
        if session.state in (SessionState.RUNNING, SessionState.AWAITING_INPUT):
            runner = get_api_runner(session.adapter)
            if payload.approval_mode is not None:
                runner.update_permission_mode(session_id, payload.approval_mode)
            else:
                # If clearing the override, we can't really change the running mode
                # The next start will use the global default
                pass

        logger.info(
            "Session approval mode updated",
            approval_mode=session.approval_mode,
        )
        return SessionResponse.from_session(session, store)


@router.post("/sessions/{session_id}/permission", response_model=OkResponse)
async def respond_permission(
    session_id: str,
    payload: PermissionResponseRequest,
    _: None = Depends(require_token),
) -> OkResponse:
    """Respond to a permission request from the agent."""
    with _session_logging_context(session_id):
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)

        # Build the result dict based on allow/deny
        if payload.allow:
            result = {
                "behavior": "allow",
                "updated_input": payload.updated_input,
            }
        else:
            result = {
                "behavior": "deny",
                "message": payload.message or "User denied permission",
            }

        # Resolve the pending permission request
        resolved = store.resolve_pending_permission(
            session_id, payload.request_id, result
        )
        if not resolved:
            raise_http_error(
                "NOT_FOUND",
                f"Permission request {payload.request_id} not found or already resolved",
                404,
            )

        logger.info(
            "Permission response received",
            request_id=payload.request_id,
            allow=payload.allow,
        )
        return OkResponse()


@router.post("/sessions/{session_id}/events", response_model=OkResponse)
async def push_agent_event(
    session_id: str,
    payload: AgentEventRequest,
    _: None = Depends(require_token),
) -> OkResponse:
    """Push an event from an external agent through the store event pipeline."""
    import asyncio
    import uuid

    with _session_logging_context(session_id):
        async with session_lock(session_id):
            session = store.get_session(session_id)
            if not session:
                raise_http_error("NOT_FOUND", "Session not found", 404)

            # Auto-transition CREATED -> RUNNING on first event
            if session.state == SessionState.CREATED:
                transition(session, SessionState.RUNNING, started_at=True)
                await emit_state(session)

            if payload.type == "output":
                text = payload.data.get("text", "")
                kind = payload.data.get("kind", "step")
                is_final = payload.data.get("is_final")
                await emit_output(session, text, kind=kind, is_final=is_final)

            elif payload.type == "status":
                status = payload.data.get("status", "")
                status_map = {
                    "running": SessionState.RUNNING,
                    "awaiting_input": SessionState.AWAITING_INPUT,
                    "done": SessionState.AWAITING_INPUT,
                    "error": SessionState.ERROR,
                }
                target_state = status_map.get(status)
                if target_state and target_state != session.state:
                    ended = target_state in (SessionState.ERROR,)
                    transition(session, target_state, allow_same=True, ended_at=ended)
                    await emit_state(session)

            elif payload.type == "error":
                code = payload.data.get("code", "AGENT_ERROR")
                message = payload.data.get("message", "Unknown error")
                if session.state != SessionState.ERROR:
                    transition(session, SessionState.ERROR, ended_at=True)
                    await emit_state(session)
                await emit_error(session, code, message)

            elif payload.type == "permission_request":
                request_id = payload.data.get("request_id") or f"perm_{uuid.uuid4().hex[:8]}"
                tool_name = payload.data.get("tool_name", "approval")
                tool_input = payload.data.get("tool_input", payload.data)
                future = asyncio.get_event_loop().create_future()
                store.add_pending_permission(
                    session_id, request_id, tool_name, tool_input, future
                )
                await emit_permission_request(
                    session,
                    request_id=request_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                )

            session.last_activity_at = now()
            store.update_session(session)
            logger.info("Agent event processed", event_type=payload.type)
            return OkResponse()


@router.get("/sessions/{session_id}/events/poll")
async def poll_agent_events(
    session_id: str,
    since_seq: int = 0,
    types: str | None = None,
    _: None = Depends(require_token),
) -> dict:
    """Poll for events relevant to an external agent.

    Args:
        session_id: Session to poll.
        since_seq: Only return events after this sequence number.
        types: Comma-separated event types to filter (e.g. "user_input,permission_resolved").
    """
    with _session_logging_context(session_id):
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)

        events = store.read_event_log(session_id, since_seq=since_seq)

        # Default to agent-relevant event types
        type_filter = {"user_input", "permission_resolved"}
        if types:
            type_filter = {t.strip() for t in types.split(",")}

        filtered = []
        for evt in events:
            if evt.get("type") in type_filter:
                filtered.append({
                    "type": evt["type"],
                    "data": evt.get("data", {}),
                    "seq": evt.get("seq"),
                })

        return {"events": filtered}
