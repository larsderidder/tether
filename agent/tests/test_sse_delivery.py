"""SSE delivery recovery tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from tether.sse import sse_stream
from tether.store import SessionStore


def _event_from_sse(payload: bytes) -> dict:
    """Decode one SSE event payload."""
    text = payload.decode("utf-8")
    assert text.startswith("data: ")
    return json.loads(text.removeprefix("data: ").strip())


@pytest.mark.anyio
async def test_sse_gap_replays_oldest_dropped_queue_event(
    fresh_store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow SSE client catches up from the event log after queue overflow."""
    import tether.sse as sse_module

    session = fresh_store.create_session("test", "main")
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)

    def small_subscriber(session_id: str) -> asyncio.Queue:
        fresh_store._get_runtime(session_id).subscribers.append(queue)
        return queue

    monkeypatch.setattr(sse_module, "store", fresh_store)
    monkeypatch.setattr(fresh_store, "new_subscriber", small_subscriber)
    stream = sse_stream(session.id)
    try:
        first_wait = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.02)
        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "seq": 1,
                "type": "output",
                "data": {"text": "one"},
            },
        )
        first = _event_from_sse(await asyncio.wait_for(first_wait, timeout=1.0))

        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "seq": 2,
                "type": "output",
                "data": {"text": "two"},
            },
        )
        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "seq": 3,
                "type": "output",
                "data": {"text": "three"},
            },
        )

        second = _event_from_sse(await asyncio.wait_for(anext(stream), timeout=1.0))
        third = _event_from_sse(await asyncio.wait_for(anext(stream), timeout=1.0))
    finally:
        await stream.aclose()

    assert [first["seq"], second["seq"], third["seq"]] == [1, 2, 3]
