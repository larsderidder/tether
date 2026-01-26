"""Session lifecycle endpoints."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends

from tether.api.deps import require_token
from tether.api.diff import build_git_diff
from tether.api.emit import emit_state, emit_user_input, emit_warning
from tether.api.errors import raise_http_error
from tether.api.runner_events import get_api_runner
from tether.api.schemas import (
    CreateSessionRequest,
    DiffResponse,
    InputRequest,
    OkResponse,
    RenameSessionRequest,
    SessionResponse,
    StartSessionRequest,
)
from tether.api.state import maybe_set_session_name, now, transition
from tether.diff import parse_git_diff
from tether.discovery.running import is_claude_session_running
from tether.git import has_git_repository, normalize_directory_path
from tether.models import SessionState
from tether.runner import get_runner_type
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
    )
    normalized_directory: str | None = None
    if payload.directory:
        candidate = Path(payload.directory).expanduser()
        if not candidate.is_dir():
            raise_http_error("VALIDATION_ERROR", "directory must be an existing folder", 422)
        normalized_directory = normalize_directory_path(payload.directory)
    resolved_repo_id = payload.repo_id or normalized_directory or "repo_local"
    session = store.create_session(repo_id=resolved_repo_id, base_ref=payload.base_ref)
    if normalized_directory:
        session.repo_display = normalized_directory
        store.update_session(session)
        store.set_workdir(session.id, normalized_directory, managed=False)
    session = store.get_session(session.id) or session
    with _session_logging_context(session.id):
        logger.info(
            "Session created",
            repo_id=session.repo_id,
            directory=normalized_directory,
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
        store.delete_session(session_id)
        logger.info("Session deleted")
        return OkResponse()


@router.post("/sessions/{session_id}/start", response_model=SessionResponse)
async def start_session(
    session_id: str,
    payload: StartSessionRequest,
    _: None = Depends(require_token),
) -> SessionResponse:
    """Start a session and launch the configured runner."""
    with _session_logging_context(session_id):
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

        session.runner_type = get_runner_type()
        if not store.get_workdir(session_id):
            store.set_workdir(session_id, session.directory, managed=False)
        maybe_set_session_name(session, prompt)

        # Transition to RUNNING and attempt to start the runner
        transition(session, SessionState.RUNNING, started_at=True)
        await emit_state(session)

        try:
            if prompt:
                await emit_user_input(session, prompt)
            await get_api_runner().start(session_id, prompt, approval_choice)
            logger.info("Session started")
        except Exception as exc:
            # Revert to ERROR state if runner fails to start
            logger.exception("Runner failed to start", session_id=session_id)
            transition(session, SessionState.ERROR, ended_at=True)
            await emit_state(session)
            raise_http_error("RUNNER_ERROR", f"Failed to start runner: {exc}", 500)

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
        text = payload.text
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)
        if session.state not in (SessionState.RUNNING, SessionState.AWAITING_INPUT):
            raise_http_error("INVALID_STATE", "Session not running or awaiting input", 409)
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

        # Transition from AWAITING_INPUT to RUNNING when user provides input
        if session.state == SessionState.AWAITING_INPUT:
            transition(session, SessionState.RUNNING)
            await emit_state(session)
        maybe_set_session_name(session, text)
        await emit_user_input(session, text)
        await get_api_runner().send_input(session_id, text)
        session = store.get_session(session_id)
        if session:
            session.last_activity_at = now()
            store.update_session(session)
        logger.info("Session input forwarded")
        return SessionResponse.from_session(session, store)


@router.post("/sessions/{session_id}/interrupt", response_model=SessionResponse)
async def interrupt_session(session_id: str, _: None = Depends(require_token)) -> SessionResponse:
    """Interrupt the current turn. Session remains active and can continue with new input."""
    with _session_logging_context(session_id):
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
        await get_api_runner().stop(session_id)
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)
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
