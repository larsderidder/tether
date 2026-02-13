"""SSE event endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from tether.api.deps import require_token
from tether.api.errors import raise_http_error
from tether.sse import stream_response
from tether.store import store

router = APIRouter(tags=["events"])


@router.get("/events/sessions/{session_id}")
async def events(
    session_id: str,
    since: int = Query(0, ge=0),
    limit: int | None = Query(500, ge=1, le=5000),
    _: None = Depends(require_token),
):
    """SSE stream for a session's events."""
    session = store.get_session(session_id)
    if not session:
        raise_http_error("NOT_FOUND", "Session not found", 404)
    return stream_response(session_id, since_seq=since, limit=limit)
