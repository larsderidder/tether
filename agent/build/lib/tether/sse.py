"""Helpers for producing server-sent event (SSE) responses."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import structlog
from starlette.responses import StreamingResponse

from tether.store import store

logger = structlog.get_logger(__name__)

SSE_KEEPALIVE_SECONDS = 15.0


def _now() -> str:
    """Return an ISO8601 UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sse_event(data: dict) -> str:
    """Serialize an event payload into SSE wire format."""
    payload = json.dumps(data, separators=(",", ":"))
    return f"data: {payload}\n\n"


async def sse_stream(
    session_id: str, *, since_seq: int = 0, limit: int | None = None
) -> AsyncIterator[bytes]:
    """Stream SSE events for a session as UTF-8 bytes."""
    logger.debug("SSE stream started", session_id=session_id, since_seq=since_seq)
    queue = store.new_subscriber(session_id)
    heartbeat_s = SSE_KEEPALIVE_SECONDS
    last_seq = since_seq
    # Get pending permission IDs upfront for filtering stale permission_request events
    pending_permission_ids = {
        p.request_id for p in store.get_all_pending_permissions(session_id)
    }

    try:
        for event in store.read_event_log(session_id, since_seq=since_seq, limit=limit):
            seq = int(event.get("seq") or 0)
            if seq and seq <= last_seq:
                continue
            if seq:
                last_seq = seq

            # Skip permission_request events that no longer have pending permissions
            # (e.g., after backend restart or timeout)
            if event.get("type") == "permission_request":
                data = event.get("data", {})
                request_id = data.get("request_id")
                if request_id and request_id not in pending_permission_ids:
                    logger.debug(
                        "Skipping stale permission_request during replay",
                        session_id=session_id,
                        request_id=request_id,
                    )
                    continue

            yield sse_event(event).encode("utf-8")

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"
                continue
            seq = int(event.get("seq") or 0)
            if seq and seq <= last_seq:
                continue
            if seq:
                last_seq = seq
            logger.debug("Yielding live event", session_id=session_id, event_type=event.get("type"))
            yield sse_event(event).encode("utf-8")
    finally:
        logger.debug("SSE stream ended", session_id=session_id)
        store.remove_subscriber(session_id, queue)


def stream_response(
    session_id: str, *, since_seq: int = 0, limit: int | None = None
) -> StreamingResponse:
    """Build a StreamingResponse for the session SSE feed."""
    return StreamingResponse(
        sse_stream(session_id, since_seq=since_seq, limit=limit),
        media_type="text/event-stream",
    )
