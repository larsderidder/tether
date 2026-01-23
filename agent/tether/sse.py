"""Helpers for producing server-sent event (SSE) responses."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from starlette.responses import StreamingResponse

from tether.store import store

SSE_KEEPALIVE_SECONDS = 15.0


def sse_event(data: dict) -> str:
    """Serialize an event payload into SSE wire format."""
    payload = json.dumps(data, separators=(",", ":"))
    return f"data: {payload}\n\n"


async def sse_stream(
    session_id: str, *, since_seq: int = 0, limit: int | None = None
) -> AsyncIterator[bytes]:
    """Stream SSE events for a session as UTF-8 bytes."""
    queue = store.new_subscriber(session_id)
    heartbeat_s = SSE_KEEPALIVE_SECONDS
    last_seq = since_seq
    try:
        for event in store.read_event_log(session_id, since_seq=since_seq, limit=limit):
            seq = int(event.get("seq") or 0)
            if seq and seq <= last_seq:
                continue
            if seq:
                last_seq = seq
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
            yield sse_event(event).encode("utf-8")
    finally:
        store.remove_subscriber(session_id, queue)


def stream_response(
    session_id: str, *, since_seq: int = 0, limit: int | None = None
) -> StreamingResponse:
    """Build a StreamingResponse for the session SSE feed."""
    return StreamingResponse(
        sse_stream(session_id, since_seq=since_seq, limit=limit),
        media_type="text/event-stream",
    )
