"""Session state helpers shared across API modules."""

from __future__ import annotations

from tether.api.errors import raise_http_error
from tether.models import Session, SessionState
from tether.store import store

_VALID_TRANSITIONS = {
    SessionState.CREATED: {SessionState.RUNNING},
    SessionState.RUNNING: {SessionState.AWAITING_INPUT, SessionState.STOPPING, SessionState.STOPPED, SessionState.ERROR},
    SessionState.AWAITING_INPUT: {SessionState.RUNNING, SessionState.STOPPING, SessionState.STOPPED, SessionState.ERROR},
    SessionState.STOPPING: {SessionState.STOPPED, SessionState.ERROR},
    SessionState.STOPPED: {SessionState.RUNNING},
    SessionState.ERROR: {SessionState.RUNNING},
}


def now() -> str:
    """Shared timestamp helper to keep API and events consistent."""
    return store._now()


def transition(
    session: Session,
    new_state: SessionState,
    *,
    allow_same: bool = False,
    started_at: bool = False,
    ended_at: bool = False,
    exit_code: int | None = None,
) -> None:
    """Validate and apply a session state transition.

    Args:
        session: Session to update.
        new_state: Desired lifecycle state.
        allow_same: Allow no-op transitions when True.
        started_at: Set started_at timestamp when True.
        ended_at: Set ended_at timestamp when True.
        exit_code: Exit code to record, if available.
    """
    if session.state == new_state:
        if not allow_same:
            raise_http_error("INVALID_STATE", f"Session already {new_state}", 409)
    elif new_state not in _VALID_TRANSITIONS.get(session.state, set()):
        raise_http_error(
            "INVALID_STATE",
            f"Invalid state transition {session.state} -> {new_state}",
            409,
        )
    timestamp = now()
    session.state = new_state
    session.last_activity_at = timestamp
    if started_at:
        session.started_at = timestamp
    if ended_at:
        session.ended_at = timestamp
    if exit_code is not None:
        session.exit_code = exit_code
    store.update_session(session)


def maybe_set_session_name(session: Session, prompt: str) -> None:
    """Set the session name from the first non-empty prompt or input.

    Args:
        session: Session to update.
        prompt: Candidate text used to derive a name.
    """
    if session.name:
        return
    title = (prompt or "").strip()
    if not title:
        return
    title = " ".join(title.split())
    session.name = title[:80]
    store.update_session(session)
