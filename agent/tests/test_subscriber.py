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
    fresh_store: SessionStore, fake_bridge: FakeBridge
) -> BridgeSubscriber:
    """Create a BridgeSubscriber wired to a BridgeManager with the fake bridge registered."""
    mgr = BridgeManager()
    mgr.register_bridge("fake", fake_bridge)
    return BridgeSubscriber(
        bridge_manager=mgr,
        new_subscriber=fresh_store.new_subscriber,
        remove_subscriber=fresh_store.remove_subscriber,
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


class TestEventRouting:
    """Test _consume routes events to the correct bridge methods."""

    async def _emit_and_wait(
        self, store: SessionStore, session_id: str, event: dict
    ) -> None:
        await store.emit(session_id, event)
        await asyncio.sleep(0.05)

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
    async def test_automation_message_output_flushes_immediately(
        self, fresh_store: SessionStore, fake_bridge: FakeBridge
    ) -> None:
        """Automation messages are delivered as separate bridge replies."""

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
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tool telemetry is buffered briefly and then sent as one bridge message."""
        monkeypatch.setattr("tether.bridges.subscriber._TOOL_FLUSH_DELAY_S", 0.01)
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
        await sub.unsubscribe(session.id)

        assert len(fake_bridge.output_calls) == 1
        assert fake_bridge.output_calls[0]["metadata"]["tool_activity"] is True
        assert fake_bridge.output_calls[0]["metadata"]["bridge_segments"] == [
            {"kind": "tool_output", "label": "bash", "text": "pwd"},
            {"kind": "tool_output", "label": "bash", "text": "/tmp/demo"},
        ]

    @pytest.mark.anyio
    async def test_tool_activity_can_wait_until_final_output(
        self,
        fresh_store: SessionStore,
        fake_bridge: FakeBridge,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tool telemetry can be held until the final assistant message."""
        monkeypatch.setenv("TETHER_BRIDGE_TOOL_ACTIVITY_FLUSH_ON_FINAL_ONLY", "1")
        monkeypatch.setattr("tether.bridges.subscriber._TOOL_FLUSH_DELAY_S", 0.01)
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
            "pwd/tmp/demo",
            "Done.",
        ]
        assert fake_bridge.output_calls[0]["metadata"]["tool_activity"] is True

    @pytest.mark.anyio
    async def test_tool_activity_flush_timer_is_not_reset_by_more_tool_events(
        self,
        fresh_store: SessionStore,
        fake_bridge: FakeBridge,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tool telemetry flushes periodically while more tool output arrives."""
        monkeypatch.setattr("tether.bridges.subscriber._TOOL_FLUSH_DELAY_S", 0.02)
        session = fresh_store.create_session("test", "main")
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
        await sub.unsubscribe(session.id)
        assert len(fake_bridge.status_calls) == 1
        assert fake_bridge.status_calls[0]["status"] == "error"

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
        sub = _make_subscriber(fresh_store, fake_bridge)

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

        assert len(fake_bridge.output_calls) == 1
        assert fake_bridge.output_calls[0]["text"] == "recovery message"

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
