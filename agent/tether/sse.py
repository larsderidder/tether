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


def _event_seq(event: dict) -> int:
    """Return the integer sequence for a persisted event."""
    try:
        return int(event.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def _is_stale_permission_request(event: dict, pending_permission_ids: set[str]) -> bool:
    """Return true when a replayed permission request has already resolved."""
    if event.get("type") != "permission_request":
        return False
    data = event.get("data", {})
    request_id = data.get("request_id")
    return bool(request_id and request_id not in pending_permission_ids)


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
            seq = _event_seq(event)
            if seq and seq <= last_seq:
                continue
            if seq:
                last_seq = seq

            if _is_stale_permission_request(event, pending_permission_ids):
                logger.debug(
                    "Skipping stale permission_request during replay",
                    session_id=session_id,
                    request_id=(event.get("data") or {}).get("request_id"),
                )
                continue

            yield sse_event(event).encode("utf-8")

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"
                continue
            seq = _event_seq(event)
            if seq and seq <= last_seq:
                continue
            if seq and seq > last_seq + 1:
                gap_pending_permission_ids = {
                    p.request_id for p in store.get_all_pending_permissions(session_id)
                }
                for replayed in store.read_event_log(
                    session_id, since_seq=last_seq, before_seq=seq
                ):
                    replayed_seq = _event_seq(replayed)
                    if replayed_seq and replayed_seq <= last_seq:
                        continue
                    if _is_stale_permission_request(
                        replayed, gap_pending_permission_ids
                    ):
                        continue
                    if replayed_seq:
                        last_seq = replayed_seq
                    yield sse_event(replayed).encode("utf-8")
            if seq:
                last_seq = seq
            logger.debug(
                "Yielding live event",
                session_id=session_id,
                event_type=event.get("type"),
            )
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
