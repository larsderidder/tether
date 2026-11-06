"""Tests for BridgeSubscriber event routing logic."""

from __future__ import annotations

import asyncio

import pytest

from tether.bridges.base import ApprovalRequest, BridgeInterface
from agent_tether.manager import BridgeManager
from tether.bridges.subscriber import BridgeSubscriber
from tether.store import SessionStore


class FakeBridge(BridgeInterface):
    """Minimal bridge that records all calls for assertion."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self.output_calls: list[dict] = []
        self.approval_calls: list[dict] = []
        self.status_calls: list[dict] = []
        self.typing_calls: list[str] = []
        self.removed_calls: list[str] = []

    async def on_output(
        self, session_id: str, text: str, metadata: dict | None = None
    ) -> None:
        call = {"session_id": session_id, "text": text, "metadata": metadata}
        self.output_calls.append(call)
        self.calls.append(("output", call))

    async def on_approval_request(
        self, session_id: str, request: ApprovalRequest
    ) -> None:
        call = {"session_id": session_id, "request": request}
        self.approval_calls.append(call)
        self.calls.append(("approval", call))

    async def on_status_change(
        self, session_id: str, status: str, metadata: dict | None = None
    ) -> None:
        self.status_calls.append(
            {"session_id": session_id, "status": status, "metadata": metadata}
        )

    async def create_thread(
        self,
        session_id: str,
        session_name: str,
        existing_thread_id: str | None = None,
    ) -> dict:
        return {"thread_id": f"t_{session_id}", "platform": "fake"}

    async def on_typing(self, session_id: str) -> None:
        self.typing_calls.append(session_id)

    async def on_session_removed(self, session_id: str) -> None:
        await super().on_session_removed(session_id)
        self.removed_calls.append(session_id)


@pytest.fixture
def fake_bridge():
    return FakeBridge()


def _make_subscriber(
    fresh_store: SessionStore,
    fake_bridge: FakeBridge,
    *,
    outbox_retry_delay_s: float = 5.0,
) -> BridgeSubscriber:
    """Create a BridgeSubscriber wired to a BridgeManager with the fake bridge registered."""
    mgr = BridgeManager()
    mgr.register_bridge("fake", fake_bridge)
    return BridgeSubscriber(
        bridge_manager=mgr,
        new_subscriber=fresh_store.new_subscriber,
        remove_subscriber=fresh_store.remove_subscriber,
        outbox_retry_delay_s=outbox_retry_delay_s,
    )


class TestSubscriberLifecycle:
    """Test subscribe/unsubscribe task management."""

    @pytest.mark.anyio
    async def test_subscribe_creates_task(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        assert session.id in sub._tasks
        assert not sub._tasks[session.id].done()
        await sub.unsubscribe(session.id)

    @pytest.mark.anyio
    async def test_subscribe_idempotent(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        task1 = sub._tasks[session.id]
        sub.subscribe(session.id, "fake")
        task2 = sub._tasks[session.id]
        assert task1 is task2
        await sub.unsubscribe(session.id)

    @pytest.mark.anyio
    async def test_unsubscribe_removes_task(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """unsubscribe() removes task from tracking dict."""
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await sub.unsubscribe(session.id)
        assert session.id not in sub._tasks

    @pytest.mark.anyio
    async def test_unsubscribe_calls_on_session_removed(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await sub.unsubscribe(session.id, platform="fake")
        assert session.id in fake_bridge.removed_calls

    @pytest.mark.anyio
    async def test_unsubscribe_without_platform_skips_removal(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await sub.unsubscribe(session.id)
        assert session.id not in fake_bridge.removed_calls

    @pytest.mark.anyio
    async def test_unsubscribe_unknown_session_safe(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        sub = _make_subscriber(fresh_store, fake_bridge)
        await sub.unsubscribe("nonexistent")

    @pytest.mark.anyio
    async def test_unsubscribe_cancels_delayed_output_flush(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """Unsubscribing discards delayed activity instead of leaking a task."""
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        sub._buffer_output(session.id, "pending")
        await sub._schedule_flush(session.id, "fake", fake_bridge, 60)
        flush_task = sub._output_flush_tasks[session.id]

        await sub.unsubscribe(session.id)
        await asyncio.sleep(0)

        assert session.id not in sub._output_flush_tasks
        assert flush_task.cancelled()


class TestEventRouting:
    """Test _consume routes events to the correct bridge methods."""

    async def _emit_and_wait(
        self, store: SessionStore, session_id: str, event: dict
    ) -> None:
        await store.emit(session_id, event)
        await asyncio.sleep(0.05)

    @pytest.mark.anyio
    async def test_first_bridge_binding_does_not_replay_old_output(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """A new bridge binding starts at the current event-log tail."""
        session = fresh_store.create_session("test", "main")
        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "seq": fresh_store.next_seq(session.id),
                "type": "output_final",
                "data": {"text": "old transcript", "final": True},
            },
        )

        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.05)

        assert fake_bridge.output_calls == []

        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "seq": fresh_store.next_seq(session.id),
                "type": "output_final",
                "data": {"text": "new answer", "final": True},
            },
        )
        await sub.unsubscribe(session.id)

        assert [call["text"] for call in fake_bridge.output_calls] == ["new answer"]

    @pytest.mark.anyio
    async def test_restart_before_outbox_enqueue_recovers_logged_event(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """An established binding replays events logged after its durable baseline."""
        session = fresh_store.create_session("test", "main")
        fresh_store.bridge_replay_cursor(session.id, "fake")
        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "seq": fresh_store.next_seq(session.id),
                "type": "output_final",
                "data": {"text": "after baseline", "final": True},
            },
        )

        reloaded_store = SessionStore()
        sub = _make_subscriber(reloaded_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.05)
        await sub.unsubscribe(session.id)

        assert [call["text"] for call in fake_bridge.output_calls] == ["after baseline"]

    @pytest.mark.anyio
    async def test_failed_pending_delivery_survives_store_reload(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """Failed outbox rows remain pending and send after a store reload."""
        session = fresh_store.create_session("test", "main")
        delivery = fresh_store.enqueue_bridge_delivery(
            session_id=session.id,
            platform="fake",
            delivery_key="failed-pending",
            method="on_output",
            payload={"text": "retry me", "metadata": None},
            event_seq=1,
        )
        claimed = fresh_store.claim_pending_bridge_deliveries(session.id, "fake")
        assert [row.id for row in claimed] == [delivery.id]
        fresh_store.mark_bridge_delivery_failed(
            delivery.id, RuntimeError("offline"), retry_delay_s=0
        )

        reloaded_store = SessionStore()
        sub = _make_subscriber(reloaded_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.05)
        await sub.unsubscribe(session.id)

        assert [call["text"] for call in fake_bridge.output_calls] == ["retry me"]

    @pytest.mark.anyio
    async def test_final_output_event_waits_for_output_final(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {"text": "Hello world", "final": True},
            },
        )
        await sub.unsubscribe(session.id)
        assert fake_bridge.output_calls == []

    @pytest.mark.anyio
    async def test_routes_output_final_attachments_metadata(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output_final",
                "data": {
                    "text": "Final report",
                    "final": True,
                    "attachments": [
                        {
                            "path": "/tmp/report.md",
                            "filename": "report.md",
                            "title": "report.md",
                        }
                    ],
                },
            },
        )
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.output_calls) == 1
        assert (
            fake_bridge.output_calls[0]["metadata"]["attachments"][0]["filename"]
            == "report.md"
        )

    @pytest.mark.anyio
    async def test_skips_non_final_output(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {"text": "thinking step", "final": False},
            },
        )
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.output_calls) == 0

    @pytest.mark.anyio
    async def test_context_warning_flushes_immediately(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """Context warnings bypass normal end-of-turn output buffering."""
        session = fresh_store.create_session("test", "main")
        session.bridge_verbosity = "none"
        fresh_store.update_session(session)
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)

        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {
                    "text": "[warning] Pi context is 82% full\n",
                    "final": False,
                    "bridge_segments": [
                        {"kind": "warning", "text": "Pi context is 82% full"}
                    ],
                },
            },
        )
        await sub.unsubscribe(session.id)

        assert len(fake_bridge.output_calls) == 1
        assert "82%" in fake_bridge.output_calls[0]["text"]

    @pytest.mark.anyio
    async def test_automation_message_output_flushes_immediately(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """Automation messages are delivered as separate bridge replies."""

        session = fresh_store.create_session("test", "main")
        session.bridge_buffer_max_seconds = 0
        fresh_store.update_session(session)
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {
                    "text": "Working on it",
                    "final": False,
                    "bridge_segments": [
                        {"kind": "automation_message", "text": "Working on it"}
                    ],
                },
            },
        )
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.output_calls) == 1
        assert fake_bridge.output_calls[0]["text"] == "Working on it"
        assert fake_bridge.output_calls[0]["metadata"]["bridge_segments"] == [
            {"kind": "automation_message", "text": "Working on it"}
        ]

    @pytest.mark.anyio
    async def test_skips_structured_prose_final_before_output_final(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """Pi structured final prose should not duplicate output_final."""

        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {
                    "text": "same answer",
                    "kind": "final",
                    "final": True,
                    "bridge_segments": [{"kind": "assistant", "text": "same answer"}],
                },
            },
        )
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output_final",
                "data": {"text": "same answer"},
            },
        )
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.output_calls) == 1
        assert fake_bridge.output_calls[0]["text"] == "same answer"

    @pytest.mark.anyio
    async def test_duplicate_output_final_only_sent_once(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """A bridge turn can publish only one final assistant message."""
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        event = {
            "session_id": session.id,
            "type": "output_final",
            "data": {"text": "same final", "turn_id": "turn-1"},
        }
        await self._emit_and_wait(fresh_store, session.id, event)
        await self._emit_and_wait(fresh_store, session.id, event)
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.output_calls) == 1
        assert fake_bridge.output_calls[0]["text"] == "same final"
        assert fake_bridge.output_calls[0]["metadata"]["turn_id"] == "turn-1"

    @pytest.mark.anyio
    async def test_running_state_starts_new_bridge_turn(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """A new RUNNING state permits the next final answer to be delivered."""
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output_final",
                "data": {"text": "first", "turn_id": "turn-1"},
            },
        )
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "session_state",
                "data": {"state": "RUNNING"},
            },
        )
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output_final",
                "data": {"text": "second", "turn_id": "turn-2"},
            },
        )
        await sub.unsubscribe(session.id)
        assert [call["text"] for call in fake_bridge.output_calls] == [
            "first",
            "second",
        ]

    @pytest.mark.anyio
    async def test_routes_output_final_blob(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output_final",
                "data": {"text": "accumulated blob"},
            },
        )
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.output_calls) == 1
        assert fake_bridge.output_calls[0]["text"] == "accumulated blob"
        assert fake_bridge.output_calls[0]["metadata"]["final"] is True

    @pytest.mark.anyio
    async def test_minimal_verbosity_buffers_thinking_until_final_output(
        self,
        fresh_store: SessionStore,
        fake_bridge: FakeBridge,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The default-style minimal policy sends thinking and final output only."""
        monkeypatch.setenv("TETHER_BRIDGE_VERBOSITY", "minimal")
        monkeypatch.delenv("TETHER_BRIDGE_BUFFER_MAX_SECONDS", raising=False)
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        for segment in [
            {"kind": "thinking", "label": "thinking", "text": "checking state"},
            {"kind": "tool_call", "label": "bash", "text": "pwd"},
            {"kind": "tool_output", "label": "bash", "text": "/tmp/demo"},
            {"kind": "assistant", "text": "interim prose"},
        ]:
            await self._emit_and_wait(
                fresh_store,
                session.id,
                {
                    "session_id": session.id,
                    "type": "output",
                    "data": {
                        "text": segment["text"],
                        "final": False,
                        "bridge_segments": [segment],
                    },
                },
            )
        await asyncio.sleep(0.03)
        assert fake_bridge.output_calls == []

        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output_final",
                "data": {"text": "Done."},
            },
        )
        await sub.unsubscribe(session.id)

        assert [call["text"] for call in fake_bridge.output_calls] == [
            "[thinking] checking state\n",
            "Done.",
        ]
        assert fake_bridge.output_calls[0]["metadata"]["bridge_segments"] == [
            {"kind": "thinking", "label": "thinking", "text": "checking state"}
        ]

    @pytest.mark.anyio
    @pytest.mark.parametrize("verbosity", ["none", "minimal", "medium", "high"])
    async def test_assistant_progress_flushes_before_tool_execution(
        self,
        fresh_store: SessionStore,
        fake_bridge: FakeBridge,
        monkeypatch: pytest.MonkeyPatch,
        verbosity: str,
    ) -> None:
        """Tool boundaries deliver complete progress prose without streaming the final."""
        monkeypatch.setenv("TETHER_BRIDGE_VERBOSITY", verbosity)
        monkeypatch.delenv("TETHER_BRIDGE_BUFFER_MAX_SECONDS", raising=False)
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        try:
            for seq, segment in enumerate(
                [
                    {"kind": "thinking", "text": "checking state"},
                    {"kind": "assistant", "text": "Checking "},
                    {"kind": "assistant", "text": "the shop."},
                    {"kind": "tool_call", "label": "bash", "text": "secret args"},
                ],
                start=1,
            ):
                await sub._route_event(
                    session.id,
                    "fake",
                    fake_bridge,
                    {
                        "seq": seq,
                        "type": "output",
                        "data": {
                            "text": segment["text"],
                            "final": False,
                            "bridge_segments": [segment],
                        },
                    },
                )
                if seq < 4:
                    assert fake_bridge.output_calls == []

            expected = [] if verbosity == "none" else ["Checking the shop."]
            assert [call["text"] for call in fake_bridge.output_calls] == expected

            await sub._route_event(
                session.id,
                "fake",
                fake_bridge,
                {
                    "seq": 5,
                    "type": "output",
                    "data": {
                        "text": "The shop is available.",
                        "bridge_segments": [
                            {"kind": "assistant", "text": "The shop is available."}
                        ],
                    },
                },
            )
            await sub._route_event(
                session.id,
                "fake",
                fake_bridge,
                {
                    "seq": 6,
                    "type": "output_final",
                    "data": {"text": "The shop is available."},
                },
            )
            prose = [
                segment["text"]
                for call in fake_bridge.output_calls
                for segment in (call["metadata"] or {}).get("bridge_segments", [])
                if segment["kind"] == "assistant"
            ]
            assert "".join(prose) == "".join(expected)
            assert fake_bridge.output_calls[-1]["text"] == "The shop is available."
        finally:
            await sub.unsubscribe(session.id)

    @pytest.mark.anyio
    async def test_medium_verbosity_keeps_tool_names_without_contents(
        self,
        fresh_store: SessionStore,
        fake_bridge: FakeBridge,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Medium verbosity reports tool calls but redacts arguments and output."""
        monkeypatch.setenv("TETHER_BRIDGE_VERBOSITY", "medium")
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        for segment in [
            {"kind": "tool_call", "label": "bash", "text": "secret args"},
            {"kind": "tool_output", "label": "bash", "text": "secret output"},
        ]:
            await self._emit_and_wait(
                fresh_store,
                session.id,
                {
                    "session_id": session.id,
                    "type": "output",
                    "data": {
                        "text": segment["text"],
                        "final": False,
                        "bridge_segments": [segment],
                    },
                },
            )
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output_final",
                "data": {"text": "Done."},
            },
        )
        await sub.unsubscribe(session.id)

        assert [call["text"] for call in fake_bridge.output_calls] == [
            "[tool: bash]",
            "Done.",
        ]
        assert fake_bridge.output_calls[0]["metadata"]["bridge_segments"] == [
            {"kind": "tool_call", "label": "bash", "text": ""}
        ]

    @pytest.mark.anyio
    async def test_output_final_flushes_buffered_tool_activity(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """Tool telemetry is bundled before the final answer."""
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {
                    "text": "[tool: bash]\n",
                    "final": False,
                    "bridge_segments": [
                        {"kind": "tool_call", "label": "bash", "text": "pwd"}
                    ],
                },
            },
        )
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output_final",
                "data": {"text": "Done."},
            },
        )
        await sub.unsubscribe(session.id)

        assert [call["text"] for call in fake_bridge.output_calls] == [
            "[tool: bash]",
            "Done.",
        ]
        assert fake_bridge.output_calls[0]["metadata"]["bridge_segments"] == [
            {"kind": "tool_call", "label": "bash", "text": "pwd"}
        ]

    @pytest.mark.anyio
    async def test_tool_activity_flushes_as_one_group_after_delay(
        self,
        fresh_store: SessionStore,
        fake_bridge: FakeBridge,
    ) -> None:
        """Tool telemetry is buffered briefly and then sent as one bridge message."""
        session = fresh_store.create_session("test", "main")
        session.bridge_buffer_max_seconds = 0.01
        fresh_store.update_session(session)
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        for text in ["pwd", "/tmp/demo"]:
            await fresh_store.emit(
                session.id,
                {
                    "session_id": session.id,
                    "type": "output",
                    "data": {
                        "text": text,
                        "final": False,
                        "bridge_segments": [
                            {"kind": "tool_output", "label": "bash", "text": text}
                        ],
                    },
                },
            )
        await asyncio.sleep(0.03)
        await sub.unsubscribe(session.id)

        assert len(fake_bridge.output_calls) == 1
        assert fake_bridge.output_calls[0]["metadata"]["stream_batch"] is True
        assert fake_bridge.output_calls[0]["metadata"]["bridge_segments"] == [
            {"kind": "tool_output", "label": "bash", "text": "pwd"},
            {"kind": "tool_output", "label": "bash", "text": "/tmp/demo"},
        ]

    @pytest.mark.anyio
    async def test_tool_activity_can_wait_until_final_output(
        self,
        fresh_store: SessionStore,
        fake_bridge: FakeBridge,
    ) -> None:
        """Tool telemetry can be held until the final assistant message."""
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        for text in ["pwd", "/tmp/demo"]:
            await fresh_store.emit(
                session.id,
                {
                    "session_id": session.id,
                    "type": "output",
                    "data": {
                        "text": text,
                        "final": False,
                        "bridge_segments": [
                            {"kind": "tool_output", "label": "bash", "text": text}
                        ],
                    },
                },
            )
        await asyncio.sleep(0.03)
        assert fake_bridge.output_calls == []

        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output_final",
                "data": {"text": "Done."},
            },
        )
        await sub.unsubscribe(session.id)

        assert [call["text"] for call in fake_bridge.output_calls] == [
            "[bash] pwd\n[bash] /tmp/demo",
            "Done.",
        ]
        assert fake_bridge.output_calls[0]["metadata"]["tool_activity"] is True

    @pytest.mark.anyio
    async def test_tool_activity_flush_timer_is_not_reset_by_more_tool_events(
        self,
        fresh_store: SessionStore,
        fake_bridge: FakeBridge,
    ) -> None:
        """Tool telemetry flushes once per interval while more output arrives."""
        session = fresh_store.create_session("test", "main")
        session.bridge_buffer_max_seconds = 0.02
        fresh_store.update_session(session)
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {
                    "text": "pwd",
                    "final": False,
                    "bridge_segments": [
                        {"kind": "tool_output", "label": "bash", "text": "pwd"}
                    ],
                },
            },
        )
        await asyncio.sleep(0.01)
        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {
                    "text": "/tmp/demo",
                    "final": False,
                    "bridge_segments": [
                        {"kind": "tool_output", "label": "bash", "text": "/tmp/demo"}
                    ],
                },
            },
        )
        await asyncio.sleep(0.02)
        await sub.unsubscribe(session.id)

        assert len(fake_bridge.output_calls) == 1
        assert fake_bridge.output_calls[0]["metadata"]["bridge_segments"] == [
            {"kind": "tool_output", "label": "bash", "text": "pwd"},
            {"kind": "tool_output", "label": "bash", "text": "/tmp/demo"},
        ]

    @pytest.mark.anyio
    async def test_tool_activity_flushes_as_one_group_at_turn_end(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """Tool-only turns are grouped into one bridge message."""
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        for text in ["pwd", "/tmp/demo"]:
            await self._emit_and_wait(
                fresh_store,
                session.id,
                {
                    "session_id": session.id,
                    "type": "output",
                    "data": {
                        "text": text,
                        "final": False,
                        "bridge_segments": [
                            {"kind": "tool_output", "label": "bash", "text": text}
                        ],
                    },
                },
            )
        assert fake_bridge.output_calls == []

        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "session_state",
                "data": {"state": "AWAITING_INPUT"},
            },
        )
        await sub.unsubscribe(session.id)

        assert len(fake_bridge.output_calls) == 1
        assert fake_bridge.output_calls[0]["metadata"]["bridge_segments"] == [
            {"kind": "tool_output", "label": "bash", "text": "pwd"},
            {"kind": "tool_output", "label": "bash", "text": "/tmp/demo"},
        ]

    @pytest.mark.anyio
    async def test_thinking_flushes_after_buffer_max_seconds(
        self,
        fresh_store: SessionStore,
        fake_bridge: FakeBridge,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A session buffer max sends allowed non-final activity during long turns."""
        monkeypatch.setenv("TETHER_BRIDGE_VERBOSITY", "minimal")
        session = fresh_store.create_session("test", "main")
        session.bridge_buffer_max_seconds = 0
        fresh_store.update_session(session)
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {
                    "text": "partial update",
                    "final": False,
                    "bridge_segments": [{"kind": "thinking", "text": "partial update"}],
                },
            },
        )
        await sub.unsubscribe(session.id)
        assert [call["text"] for call in fake_bridge.output_calls] == [
            "[thinking] partial update\n"
        ]

    @pytest.mark.anyio
    async def test_output_final_replaces_buffered_streaming_prose(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {
                    "text": "- broken missing newline next",
                    "final": False,
                    "bridge_segments": [
                        {"kind": "assistant", "text": "- broken missing newline next"}
                    ],
                },
            },
        )
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output_final",
                "data": {"text": "- fixed\n- list"},
            },
        )
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.output_calls) == 1
        assert fake_bridge.output_calls[0]["text"] == "- fixed\n- list"

    @pytest.mark.anyio
    async def test_permission_request_flushes_buffered_output_in_event_order(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """Buffered thinking stays before tool activity when a permission prompt arrives."""
        session = fresh_store.create_session("test", "main")
        fresh_store.add_pending_permission(
            session.id, "perm_1", "Read", {}, asyncio.get_running_loop().create_future()
        )
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        for segment in [
            {"kind": "thinking", "label": "thinking", "text": "checking state"},
            {"kind": "tool_call", "label": "kubectl_exec", "text": "{}"},
        ]:
            await self._emit_and_wait(
                fresh_store,
                session.id,
                {
                    "session_id": session.id,
                    "type": "output",
                    "data": {
                        "text": segment["text"],
                        "final": False,
                        "bridge_segments": [segment],
                    },
                },
            )
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "permission_request",
                "data": {
                    "request_id": "perm_1",
                    "tool_name": "Read",
                    "tool_input": {"path": "/tmp/test.txt"},
                },
            },
        )
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {
                    "text": "[Read] Waiting for confirmation: Read /tmp/test.txt\n",
                    "final": False,
                    "bridge_segments": [
                        {
                            "kind": "tool_output",
                            "label": "Read",
                            "text": "Waiting for confirmation: Read /tmp/test.txt",
                        }
                    ],
                },
            },
        )
        await sub.unsubscribe(session.id)

        assert [kind for kind, _ in fake_bridge.calls] == ["output", "approval"]
        assert [
            segment["kind"]
            for segment in fake_bridge.output_calls[0]["metadata"]["bridge_segments"]
        ] == ["thinking", "tool_call"]

    @pytest.mark.anyio
    async def test_routes_permission_request(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        fresh_store.add_pending_permission(
            session.id, "perm_1", "Read", {}, asyncio.get_running_loop().create_future()
        )
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "permission_request",
                "data": {
                    "request_id": "perm_1",
                    "tool_name": "Read",
                    "tool_input": {"path": "/tmp/test.txt"},
                },
            },
        )
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.approval_calls) == 1
        req = fake_bridge.approval_calls[0]["request"]
        assert isinstance(req, ApprovalRequest)
        assert req.request_id == "perm_1"
        assert req.title == "Read"
        assert "/tmp/test.txt" in req.description

    @pytest.mark.anyio
    async def test_routes_session_state_running_to_typing(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "session_state",
                "data": {"state": "RUNNING"},
            },
        )
        await sub.unsubscribe(session.id)
        assert session.id in fake_bridge.typing_calls

    @pytest.mark.anyio
    async def test_routes_session_state_error_to_status(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "session_state",
                "data": {"state": "ERROR"},
            },
        )
        await asyncio.sleep(0.1)
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.status_calls) == 1
        assert fake_bridge.status_calls[0]["status"] == "error"

    @pytest.mark.anyio
    async def test_coalesces_error_state_with_detailed_error(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """The detailed error replaces the preceding generic error state."""
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)

        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "type": "session_state",
                "data": {"state": "ERROR"},
            },
        )
        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "type": "error",
                "data": {"message": "WebSocket error"},
            },
        )
        await asyncio.sleep(0.15)
        await sub.unsubscribe(session.id)

        assert fake_bridge.status_calls == [
            {
                "session_id": session.id,
                "status": "error",
                "metadata": {"message": "WebSocket error"},
            }
        ]

    @pytest.mark.anyio
    async def test_routes_error_event_to_status(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "error",
                "data": {"message": "Process crashed"},
            },
        )
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.status_calls) == 1
        assert fake_bridge.status_calls[0]["status"] == "error"
        assert fake_bridge.status_calls[0]["metadata"]["message"] == "Process crashed"

    @pytest.mark.anyio
    async def test_skips_history_events(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {"text": "old history", "final": True, "is_history": True},
            },
        )
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.output_calls) == 0

    @pytest.mark.anyio
    async def test_skips_empty_output_text(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output",
                "data": {"text": "", "final": True},
            },
        )
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.output_calls) == 0

    @pytest.mark.anyio
    async def test_no_bridge_exits_gracefully(self, fresh_store: SessionStore) -> None:
        session = fresh_store.create_session("test", "main")
        mgr = BridgeManager()  # No bridges registered
        sub = BridgeSubscriber(
            bridge_manager=mgr,
            new_subscriber=fresh_store.new_subscriber,
            remove_subscriber=fresh_store.remove_subscriber,
        )
        sub.subscribe(session.id, "nonexistent")
        await asyncio.sleep(0.05)
        task = sub._tasks.get(session.id)
        if task:
            assert task.done()

    @pytest.mark.anyio
    async def test_bridge_error_does_not_crash_consumer(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """If bridge.on_output raises, consumer continues processing."""
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge, outbox_retry_delay_s=0)

        call_count = 0
        original_on_output = fake_bridge.on_output

        async def flaky_output(session_id: str, text: str, metadata=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Simulated bridge failure")
            await original_on_output(session_id, text, metadata)

        fake_bridge.on_output = flaky_output

        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)

        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output_final",
                "data": {"text": "failing message", "final": True},
            },
        )
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "output_final",
                "data": {"text": "recovery message", "final": True},
            },
        )

        await sub.unsubscribe(session.id)

        assert [call["text"] for call in fake_bridge.output_calls] == [
            "failing message",
            "recovery message",
        ]

    @pytest.mark.anyio
    async def test_session_state_awaiting_input_ignored(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """AWAITING_INPUT state triggers neither typing nor status."""
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await self._emit_and_wait(
            fresh_store,
            session.id,
            {
                "session_id": session.id,
                "type": "session_state",
                "data": {"state": "AWAITING_INPUT"},
            },
        )
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.typing_calls) == 0
        assert len(fake_bridge.status_calls) == 0

    @pytest.mark.anyio
    async def test_full_live_queue_preserves_tail_event_without_reconnect(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """The newest event must wake the subscriber and trigger persisted catchup."""
        session = fresh_store.create_session("test", "main")
        sub = _make_subscriber(fresh_store, fake_bridge)
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        fresh_store._get_runtime(session.id).subscribers.append(queue)
        first_send_started = asyncio.Event()
        release_first_send = asyncio.Event()
        original_on_output = fake_bridge.on_output

        async def blocking_output(session_id: str, text: str, metadata=None):
            if text == "first":
                first_send_started.set()
                await release_first_send.wait()
            await original_on_output(session_id, text, metadata)

        fake_bridge.on_output = blocking_output
        task = asyncio.create_task(sub._consume(session.id, "fake", queue))
        await asyncio.sleep(0.02)
        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "seq": 1,
                "type": "output_final",
                "data": {"text": "first", "turn_id": "turn-1"},
            },
        )
        await asyncio.wait_for(first_send_started.wait(), timeout=1.0)
        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "seq": 2,
                "type": "session_state",
                "data": {"state": "RUNNING"},
            },
        )
        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "seq": 3,
                "type": "output_final",
                "data": {"text": "tail", "turn_id": "turn-2"},
            },
        )
        release_first_send.set()
        await asyncio.sleep(0.1)
        task.cancel()
        await task

        assert [call["text"] for call in fake_bridge.output_calls] == ["first", "tail"]

    @pytest.mark.anyio
    async def test_replays_logged_event_after_queue_was_not_consumed(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """A persisted event is recovered even if the first live queue is discarded."""
        session = fresh_store.create_session("test", "main")
        fresh_store.bridge_replay_cursor(session.id, "fake")
        queue = fresh_store.new_subscriber(session.id)
        event = {
            "session_id": session.id,
            "seq": 1,
            "type": "output_final",
            "data": {"text": "recovered after cancel", "turn_id": "turn-1"},
        }
        await fresh_store.emit(session.id, event)
        fresh_store.remove_subscriber(session.id, queue)

        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.05)
        await sub.unsubscribe(session.id)

        assert [call["text"] for call in fake_bridge.output_calls] == [
            "recovered after cancel"
        ]

    @pytest.mark.anyio
    async def test_permission_resolved_before_replay_skips_prompt(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """Replay must not resurrect permission prompts that were already resolved."""
        session = fresh_store.create_session("test", "main")
        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "seq": 1,
                "type": "permission_request",
                "data": {
                    "request_id": "perm_1",
                    "tool_name": "Read",
                    "tool_input": {"path": "/tmp/a"},
                },
            },
        )
        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "seq": 2,
                "type": "permission_resolved",
                "data": {"request_id": "perm_1", "allowed": True},
            },
        )

        sub = _make_subscriber(fresh_store, fake_bridge)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.05)
        await sub.unsubscribe(session.id)

        assert fake_bridge.approval_calls == []

    @pytest.mark.anyio
    async def test_failed_outbox_retries_without_reconnect(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """A failed pending delivery is retried while the subscriber stays running."""
        session = fresh_store.create_session("test", "main")
        calls = 0
        original_on_output = fake_bridge.on_output

        async def fail_once(session_id: str, text: str, metadata=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary failure")
            await original_on_output(session_id, text, metadata)

        fake_bridge.on_output = fail_once
        sub = _make_subscriber(fresh_store, fake_bridge, outbox_retry_delay_s=0.0)
        sub.subscribe(session.id, "fake")
        await asyncio.sleep(0.02)
        await fresh_store.emit(
            session.id,
            {
                "session_id": session.id,
                "seq": 1,
                "type": "output_final",
                "data": {"text": "eventual", "turn_id": "turn-1"},
            },
        )
        await asyncio.sleep(0.1)
        await sub.unsubscribe(session.id)

        assert calls == 2
        assert [call["text"] for call in fake_bridge.output_calls] == ["eventual"]

    @pytest.mark.anyio
    async def test_concurrent_outbox_drains_send_once(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """Subscriber and history drains cannot claim the same pending row."""
        from tether.external_sync import _drain_history_outbox

        session = fresh_store.create_session("test", "main")
        session.platform = "fake"
        fresh_store.update_session(session)
        fresh_store.enqueue_bridge_delivery(
            session_id=session.id,
            platform="fake",
            delivery_key="one-output",
            method="on_output",
            payload={"text": "only once", "metadata": None},
            event_seq=1,
        )
        sub = _make_subscriber(fresh_store, fake_bridge)

        await asyncio.gather(
            sub._drain_pending_deliveries(session.id, "fake", fake_bridge),
            _drain_history_outbox(session, fake_bridge),
        )

        assert [call["text"] for call in fake_bridge.output_calls] == ["only once"]


@pytest.mark.anyio
async def test_live_buffered_events_are_processed_once(
    fresh_store: SessionStore, fake_bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unflushed durable cursor must not look like a live queue gap."""
    session = fresh_store.create_session("test", "main")
    session.bridge_verbosity = "high"
    fresh_store.update_session(session)
    sub = _make_subscriber(fresh_store, fake_bridge)
    processed: asyncio.Queue[int] = asyncio.Queue()
    original_route = sub._route_event

    async def observe_route(session_id, platform, bridge, event):
        result = await original_route(session_id, platform, bridge, event)
        processed.put_nowait(event["seq"])
        return result

    monkeypatch.setattr(sub, "_route_event", observe_route)
    sub.subscribe(session.id, "fake")
    try:
        for seq, text in enumerate(["A", "B"], start=1):
            await fresh_store.emit(
                session.id,
                {
                    "seq": seq,
                    "type": "output",
                    "data": {
                        "text": text,
                        "kind": "step",
                        "bridge_segments": [{"kind": "thinking", "text": text}],
                    },
                },
            )
            while await asyncio.wait_for(processed.get(), timeout=1) != seq:
                pass
        await sub._flush_output(session.id, "fake", fake_bridge)
        assert len(fake_bridge.output_calls) == 1
        assert [
            segment["text"]
            for segment in fake_bridge.output_calls[0]["metadata"]["bridge_segments"]
        ] == ["A", "B"]
    finally:
        await sub.unsubscribe(session.id, platform="fake")


@pytest.mark.anyio
async def test_timer_flush_does_not_cancel_its_own_platform_send(
    fresh_store: SessionStore, fake_bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed flush must survive a real asynchronous transport boundary."""
    session = fresh_store.create_session("test", "main")
    sub = _make_subscriber(fresh_store, fake_bridge)
    original_output = fake_bridge.on_output

    async def asynchronous_output(*args, **kwargs):
        await asyncio.sleep(0)
        await original_output(*args, **kwargs)

    monkeypatch.setattr(fake_bridge, "on_output", asynchronous_output)
    sub._buffer_output(session.id, "buffered", event_seq=1)
    await sub._schedule_flush(session.id, "fake", fake_bridge, 0)
    task = sub._output_flush_tasks[session.id]
    await asyncio.wait_for(task, timeout=1)
    assert [call["text"] for call in fake_bridge.output_calls] == ["buffered"]


@pytest.mark.anyio
async def test_restart_recovers_final_after_pre_final_flush_only(
    fresh_store: SessionStore, fake_bridge: FakeBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persisting a prefix delivery cannot checkpoint an incomplete final event."""

    class SimulatedCrash(BaseException):
        pass

    session = fresh_store.create_session("test", "main")
    session.bridge_verbosity = "high"
    fresh_store.update_session(session)
    fresh_store.bridge_replay_cursor(session.id, "fake")
    sub = _make_subscriber(fresh_store, fake_bridge)
    step = {
        "seq": 1,
        "type": "output",
        "data": {
            "text": "prefix",
            "kind": "step",
            "bridge_segments": [{"kind": "thinking", "text": "prefix"}],
        },
    }
    final = {
        "seq": 2,
        "type": "output_final",
        "data": {"text": "complete", "turn_id": "one"},
    }
    await fresh_store.emit(session.id, step)
    await sub._route_event(session.id, "fake", fake_bridge, step)
    await fresh_store.emit(session.id, final)
    original_enqueue = fresh_store.enqueue_bridge_delivery

    def crash_before_final(**kwargs):
        if (kwargs["payload"].get("metadata") or {}).get("final"):
            raise SimulatedCrash
        return original_enqueue(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fresh_store, "enqueue_bridge_delivery", crash_before_final)
        with pytest.raises(SimulatedCrash):
            await sub._route_event(session.id, "fake", fake_bridge, final)

    restarted_store = SessionStore()
    restarted = _make_subscriber(restarted_store, fake_bridge)
    await restarted._replay_logged_events(
        session.id,
        "fake",
        fake_bridge,
        since_seq=restarted_store.bridge_replay_cursor(session.id, "fake").cursor_seq,
    )
    assert [
        call["text"]
        for call in fake_bridge.output_calls
        if (call.get("metadata") or {}).get("final")
    ] == ["complete"]
    assert len(fake_bridge.output_calls) == 2


@pytest.mark.anyio
async def test_ignored_resolution_cannot_checkpoint_buffered_output(
    fresh_store: SessionStore, fake_bridge: FakeBridge
) -> None:
    """Events with no send still cannot advance past an unpersisted buffer."""
    session = fresh_store.create_session("test", "main")
    session.bridge_verbosity = "high"
    fresh_store.update_session(session)
    fresh_store.bridge_replay_cursor(session.id, "fake")
    sub = _make_subscriber(fresh_store, fake_bridge)
    await fresh_store.emit(
        session.id,
        {
            "seq": 1,
            "type": "output",
            "data": {
                "text": "buffered",
                "kind": "step",
                "bridge_segments": [{"kind": "thinking", "text": "buffered"}],
            },
        },
    )
    await fresh_store.emit(
        session.id,
        {"seq": 2, "type": "permission_resolved", "data": {"request_id": "old"}},
    )
    try:
        await sub._replay_logged_events(session.id, "fake", fake_bridge, since_seq=0)
        assert fresh_store.bridge_replay_cursor(session.id, "fake").cursor_seq == 0
    finally:
        await sub.unsubscribe(session.id, platform="fake")


@pytest.mark.anyio
async def test_restart_cancels_approval_without_recoverable_waiter(
    fresh_store: SessionStore, fake_bridge: FakeBridge
) -> None:
    """An orphaned durable prompt is not a usable permission decision after restart."""
    session = fresh_store.create_session("test", "main")
    fresh_store.bridge_replay_cursor(session.id, "fake")
    future = asyncio.get_running_loop().create_future()
    fresh_store.add_pending_permission(session.id, "orphan", "Edit", {}, future)
    await fresh_store.emit(
        session.id,
        {
            "seq": 1,
            "type": "permission_request",
            "data": {"request_id": "orphan", "tool_name": "Edit", "tool_input": {}},
        },
    )
    restarted_store = SessionStore()
    sub = _make_subscriber(restarted_store, fake_bridge)
    await sub._replay_logged_events(session.id, "fake", fake_bridge, since_seq=0)
    assert fake_bridge.approval_calls == []
    assert restarted_store.get_pending_permission(session.id, "orphan") is None
    future.cancel()


@pytest.mark.anyio
@pytest.mark.parametrize("replayed", [False, True], ids=["live", "replay"])
async def test_failed_enqueue_cannot_checkpoint_past_heartbeat(
    fresh_store: SessionStore,
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
    replayed: bool,
) -> None:
    """A failed route remains replayable after later events and resubscription."""
    session = fresh_store.create_session("test", "main")
    fresh_store.bridge_replay_cursor(session.id, "fake")
    sub = _make_subscriber(fresh_store, fake_bridge)
    processed: asyncio.Queue[int] = asyncio.Queue()
    original_route = sub._route_event
    original_enqueue = fresh_store.enqueue_bridge_delivery
    enqueue_attempts = 0

    async def observe_route(
        session_id: str, platform: str, bridge: FakeBridge, event: dict
    ) -> bool:
        """Wait for actual routing rather than guessing when the consumer ran."""
        result = await original_route(session_id, platform, bridge, event)
        processed.put_nowait(event["seq"])
        return result

    def fail_first_enqueue(**kwargs):
        """Fail persistence before any delivery row exists."""
        nonlocal enqueue_attempts
        enqueue_attempts += 1
        if enqueue_attempts == 1:
            raise OSError("outbox temporarily unavailable")
        return original_enqueue(**kwargs)

    monkeypatch.setattr(sub, "_route_event", observe_route)
    monkeypatch.setattr(fresh_store, "enqueue_bridge_delivery", fail_first_enqueue)
    events = [
        {"seq": 1, "type": "output_final", "data": {"text": "recover me"}},
        {"seq": 2, "type": "heartbeat", "data": {}},
    ]
    if replayed:
        for event in events:
            await fresh_store.emit(session.id, event)

    sub.subscribe(session.id, "fake")
    try:
        if not replayed:
            # Let startup replay finish before emitting genuinely live events.
            await asyncio.sleep(0)
            for event in events:
                await fresh_store.emit(session.id, event)
        for seq in (1, 2):
            assert await asyncio.wait_for(processed.get(), timeout=1) == seq

        assert enqueue_attempts == 1
        assert fake_bridge.output_calls == []
        assert fresh_store.pending_bridge_deliveries(session.id, "fake") == []
        assert fresh_store.bridge_replay_cursor(session.id, "fake").cursor_seq == 0
    finally:
        await sub.unsubscribe(session.id, platform="fake")

    sub.subscribe(session.id, "fake")
    try:
        for seq in (1, 2):
            assert await asyncio.wait_for(processed.get(), timeout=1) == seq
        assert [call["text"] for call in fake_bridge.output_calls] == ["recover me"]
        assert fresh_store.bridge_replay_cursor(session.id, "fake").cursor_seq == 2
    finally:
        await sub.unsubscribe(session.id, platform="fake")


@pytest.mark.anyio
@pytest.mark.parametrize("deferred_kind", ["output", "error"])
@pytest.mark.parametrize("enqueue_fails", [False, True], ids=["persisted", "failed"])
async def test_deferred_delivery_checkpoint_tracks_persistence(
    fresh_store: SessionStore,
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
    deferred_kind: str,
    enqueue_fails: bool,
) -> None:
    """Deferred work releases its checkpoint only after successful persistence."""
    monkeypatch.delenv("TETHER_BRIDGE_BUFFER_MAX_SECONDS", raising=False)
    session = fresh_store.create_session("test", "main")
    session.bridge_verbosity = "high"
    fresh_store.update_session(session)
    fresh_store.bridge_replay_cursor(session.id, "fake")
    sub = _make_subscriber(fresh_store, fake_bridge)
    data = (
        {
            "text": "buffered",
            "bridge_segments": [{"kind": "thinking", "text": "buffered"}],
        }
        if deferred_kind == "output"
        else {"state": "ERROR"}
    )
    await fresh_store.emit(
        session.id,
        {
            "seq": 1,
            "type": "output" if deferred_kind == "output" else "session_state",
            "data": data,
        },
    )
    await fresh_store.emit(session.id, {"seq": 2, "type": "heartbeat", "data": {}})

    def fail_enqueue(**kwargs) -> None:
        """Fail the scheduled persistence outside the event-routing handler."""
        raise OSError("outbox temporarily unavailable")

    try:
        await sub._replay_logged_events(session.id, "fake", fake_bridge, since_seq=0)
        assert fresh_store.bridge_replay_cursor(session.id, "fake").cursor_seq == 0
        if enqueue_fails:
            monkeypatch.setattr(fresh_store, "enqueue_bridge_delivery", fail_enqueue)
        if deferred_kind == "output":
            await sub._schedule_flush(session.id, "fake", fake_bridge, 0)
            task = sub._output_flush_tasks[session.id]
        else:
            task = sub._error_status_tasks[session.id]

        if enqueue_fails:
            with pytest.raises(OSError, match="outbox temporarily unavailable"):
                await asyncio.wait_for(task, timeout=1)
        else:
            await asyncio.wait_for(task, timeout=1)
            assert fresh_store.bridge_replay_cursor(session.id, "fake").cursor_seq == 1

        await fresh_store.emit(session.id, {"seq": 3, "type": "heartbeat", "data": {}})
        await sub._replay_logged_events(session.id, "fake", fake_bridge, since_seq=2)
        expected_cursor = 0 if enqueue_fails else 3
        assert (
            fresh_store.bridge_replay_cursor(session.id, "fake").cursor_seq
            == expected_cursor
        )
    finally:
        await sub.unsubscribe(session.id)
