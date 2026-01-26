"""Debug endpoints for local development."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends

from tether.api.deps import require_token
from tether.api.emit import emit_state
from tether.api.runner_events import get_api_runner
from tether.api.schemas import OkResponse
from tether.api.state import transition
from tether.models import SessionState
from tether.store import store

router = APIRouter(tags=["debug"])
logger = structlog.get_logger(__name__)


@router.post("/debug/clear_data", response_model=OkResponse)
async def clear_data(_: None = Depends(require_token)) -> OkResponse:
    """Clear all persisted sessions and event logs (debug only)."""
    for session in store.list_sessions():
        if session.state == SessionState.RUNNING:
            transition(session, SessionState.INTERRUPTING)
            await emit_state(session)
            await get_api_runner().stop(session.id)
        elif session.state == SessionState.INTERRUPTING:
            await get_api_runner().stop(session.id)
    store.clear_all_data()
    logger.warning("Cleared all session data")
    return OkResponse()
