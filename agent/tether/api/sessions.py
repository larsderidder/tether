"""Session lifecycle endpoints."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import structlog
from fastapi import APIRouter, Body, Depends

from tether.api.deps import require_token
from tether.api.diff import build_git_diff
from tether.api.emit import emit_state
from tether.api.errors import raise_http_error
from tether.api.runner_events import runner
from tether.api.state import maybe_set_session_name, now, transition
from tether.runner import get_runner_type
from tether.diff import parse_git_diff
from tether.git import has_git_repository, normalize_directory_path
from tether.models import SessionState
from tether.store import store

router = APIRouter(tags=["sessions"])
logger = structlog.get_logger("tether.api.sessions")


@contextmanager
def _session_logging_context(session_id: str):
    structlog.contextvars.bind_contextvars(session_id=session_id)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars("session_id")


@router.get("/sessions", response_model=dict)
async def list_sessions(_: None = Depends(require_token)) -> dict:
    """List all sessions in memory."""
    sessions = store.list_sessions()
    logger.info("Listed sessions", count=len(sessions))
    return {"sessions": [_serialize_session(session) for session in sessions]}


@router.post("/sessions", response_model=dict, status_code=201)
async def create_session(
    payload: dict = Body(...),
    _: None = Depends(require_token),
) -> dict:
    """Create a new session in CREATED state."""
    logger.info(
        "Create session requested",
        repo_id=payload.get("repo_id"),
        directory=payload.get("directory"),
        base_ref=payload.get("base_ref"),
    )
    repo_id = payload.get("repo_id")
    directory = payload.get("directory")
    base_ref = payload.get("base_ref")
    normalized_directory: str | None = None
    if directory:
        candidate = Path(directory).expanduser()
        if not candidate.is_dir():
            raise_http_error("VALIDATION_ERROR", "directory must be an existing folder", 422)
        normalized_directory = normalize_directory_path(directory)
    resolved_repo_id = repo_id or normalized_directory or "repo_local"
    session = store.create_session(repo_id=resolved_repo_id, base_ref=base_ref)
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
        return {"session": _serialize_session(session)}


@router.get("/sessions/{session_id}", response_model=dict)
async def get_session(session_id: str, _: None = Depends(require_token)) -> dict:
    """Fetch a single session by id."""
    with _session_logging_context(session_id):
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)
        logger.info("Fetched session", state=session.state)
        return {"session": _serialize_session(session)}


@router.delete("/sessions/{session_id}", response_model=dict)
async def delete_session(session_id: str, _: None = Depends(require_token)) -> dict:
    """Delete a session if it is not running."""
    with _session_logging_context(session_id):
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)
        if session.state in (SessionState.RUNNING, SessionState.INTERRUPTING):
            raise_http_error("INVALID_STATE", "Session is active", 409)
        store.delete_session(session_id)
        logger.info("Session deleted")
        return {"ok": True}


@router.post("/sessions/{session_id}/start", response_model=dict)
async def start_session(
    session_id: str,
    payload: dict = Body(...),
    _: None = Depends(require_token),
) -> dict:
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
        prompt = payload.get("prompt", "")
        approval_choice = payload.get("approval_choice", 1)
        if approval_choice not in (1, 2):
            raise_http_error("VALIDATION_ERROR", "approval_choice must be 1 or 2", 422)

        # Clear stale state from previous runs
        if session.state in (SessionState.AWAITING_INPUT, SessionState.ERROR):
            session.ended_at = None
            session.exit_code = None
            session.summary = None
            if hasattr(session, "runner_header"):
                session.runner_header = None
            store.clear_process(session_id)
            store.clear_master_fd(session_id)
            store.clear_stdin(session_id)
            store.clear_prompt_sent(session_id)
            store.clear_pending_inputs(session_id)
            store.clear_runner_session_id(session_id)
            store.clear_last_output(session_id)

        logger.info("Session start requested")
        session.runner_type = get_runner_type()
        store.clear_runner_session_id(session_id)
        if not store.get_workdir(session_id):
            store.set_workdir(session_id, session.directory, managed=False)
        maybe_set_session_name(session, prompt)

        # Transition to RUNNING and attempt to start the runner
        transition(session, SessionState.RUNNING, started_at=True)
        await emit_state(session)

        try:
            await runner.start(session_id, prompt, approval_choice)
            logger.info("Session started")
        except Exception as exc:
            # Revert to ERROR state if runner fails to start
            logger.exception("Runner failed to start", session_id=session_id)
            transition(session, SessionState.ERROR, ended_at=True)
            await emit_state(session)
            raise_http_error("RUNNER_ERROR", f"Failed to start runner: {exc}", 500)

        return {"session": _serialize_session(session)}


@router.patch("/sessions/{session_id}/rename", response_model=dict)
async def rename_session(
    session_id: str,
    payload: dict = Body(...),
    _: None = Depends(require_token),
) -> dict:
    """Rename an existing session."""
    with _session_logging_context(session_id):
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)
        value = payload.get("name", "")
        cleaned = " ".join(str(value).split())
        if not cleaned:
            raise_http_error("VALIDATION_ERROR", "name is required", 422)
        session.name = cleaned[:80]
        store.update_session(session)
        logger.info("Session renamed", name=session.name)
        return {"session": _serialize_session(session)}


@router.post("/sessions/{session_id}/input", response_model=dict)
async def send_input(
    session_id: str,
    payload: dict = Body(...),
    _: None = Depends(require_token),
) -> dict:
    """Send input to a running or awaiting session."""
    with _session_logging_context(session_id):
        text = payload.get("text")
        if not text:
            raise_http_error("VALIDATION_ERROR", "text is required", 422)
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)
        if session.state not in (SessionState.RUNNING, SessionState.AWAITING_INPUT):
            raise_http_error("INVALID_STATE", "Session not running or awaiting input", 409)
        logger.info("Session input received", text_length=len(text))
        # Transition from AWAITING_INPUT to RUNNING when user provides input
        if session.state == SessionState.AWAITING_INPUT:
            transition(session, SessionState.RUNNING)
            await emit_state(session)
        maybe_set_session_name(session, text)
        await runner.send_input(session_id, text)
        session = store.get_session(session_id)
        if session:
            session.last_activity_at = now()
            store.update_session(session)
        logger.info("Session input forwarded")
        return {"session": _serialize_session(session)}


@router.post("/sessions/{session_id}/interrupt", response_model=dict)
async def interrupt_session(session_id: str, _: None = Depends(require_token)) -> dict:
    """Interrupt the current turn. Session remains active and can continue with new input."""
    with _session_logging_context(session_id):
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)
        # Idempotent: already awaiting input or interrupting
        if session.state in (SessionState.AWAITING_INPUT, SessionState.INTERRUPTING):
            logger.info("Session interrupt requested but already idle/interrupting")
            return {"session": _serialize_session(session)}
        if session.state in (SessionState.CREATED, SessionState.ERROR):
            raise_http_error("INVALID_STATE", "Session not running", 409)
        # Transition to INTERRUPTING
        transition(session, SessionState.INTERRUPTING)
        await emit_state(session)
        logger.info("Interrupting session")
        await runner.stop(session_id)
        session = store.get_session(session_id)
        if not session:
            raise_http_error("NOT_FOUND", "Session not found", 404)
        # Transition to AWAITING_INPUT after interrupt completes
        if session.state == SessionState.INTERRUPTING:
            transition(session, SessionState.AWAITING_INPUT)
            await emit_state(session)
        logger.info("Session interrupted")
        return {"session": _serialize_session(session)}


@router.get("/sessions/{session_id}/diff", response_model=dict)
async def get_diff(session_id: str, _: None = Depends(require_token)) -> dict:
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
            return {"diff": diff_text, "files": files}
        logger.info("Diff unavailable", path=target, reason="no repository")
        return {"diff": "", "files": []}


def _serialize_session(session) -> dict:
    return {
        "id": session.id,
        "state": session.state.value if hasattr(session.state, "value") else session.state,
        "name": session.name,
        "created_at": session.created_at,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "last_activity_at": session.last_activity_at,
        "exit_code": session.exit_code,
        "summary": session.summary,
        "runner_header": getattr(session, "runner_header", None),
        "runner_type": getattr(session, "runner_type", None),
        "directory": session.directory,
        "directory_has_git": session.directory_has_git,
    }
