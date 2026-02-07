"""Integration tests for external agent flows.

These tests cover the most common integration failure points:
end-to-end permission round-trips, human input reaching agents,
platform validation, and bridge subscriber resilience.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from tether.bridges.base import ApprovalRequest, BridgeInterface
from tether.bridges.manager import BridgeManager, bridge_manager
from tether.bridges.subscriber import BridgeSubscriber
from tether.models import SessionState
from tether.store import SessionStore


class MockBridge(BridgeInterface):
    """Mock bridge that records all calls."""

    def __init__(self):
        self.output_calls: list[dict] = []
        self.approval_calls: list[dict] = []
        self.status_calls: list[dict] = []
        self.thread_calls: list[dict] = []
        self.output_received = asyncio.Event()
        self.approval_received = asyncio.Event()
        self.status_received = asyncio.Event()

    async def on_output(self, session_id: str, text: str, metadata: dict | None = None) -> None:
        self.output_calls.append({"session_id": session_id, "text": text, "metadata": metadata})
        self.output_received.set()

    async def on_approval_request(self, session_id: str, request: ApprovalRequest) -> None:
        self.approval_calls.append({"session_id": session_id, "request": request})
        self.approval_received.set()

    async def on_status_change(self, session_id: str, status: str, metadata: dict | None = None) -> None:
        self.status_calls.append({"session_id": session_id, "status": status, "metadata": metadata})
        self.status_received.set()

    async def create_thread(self, session_id: str, session_name: str) -> dict:
        self.thread_calls.append({"session_id": session_id, "session_name": session_name})
        return {"thread_id": f"mock_{session_id}", "platform": "mock"}


class FailingBridge(BridgeInterface):
    """Bridge that raises on every on_output call."""

    def __init__(self):
        self.output_call_count = 0
        self.status_calls: list[dict] = []
        self.status_received = asyncio.Event()

    async def on_output(self, session_id: str, text: str, metadata: dict | None = None) -> None:
        self.output_call_count += 1
        raise RuntimeError("Telegram API is down")

    async def on_approval_request(self, session_id: str, request: ApprovalRequest) -> None:
        pass

    async def on_status_change(self, session_id: str, status: str, metadata: dict | None = None) -> None:
        self.status_calls.append({"session_id": session_id, "status": status})
        self.status_received.set()

    async def create_thread(self, session_id: str, session_name: str) -> dict:
        return {"thread_id": f"fail_{session_id}", "platform": "failing"}


async def _wait(event: asyncio.Event, timeout: float = 2.0) -> None:
    """Wait for an event with a timeout, failing clearly if it doesn't fire."""
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pytest.fail(f"Timed out waiting for event (waited {timeout}s)")


class TestPermissionRoundTrip:
    """End-to-end: agent pushes permission_request, bridge resolves it, agent polls the result."""

    @pytest.mark.anyio
    async def test_full_permission_flow(self, api_client, fresh_store: SessionStore) -> None:
        """Permission request → resolve via /permission → poll returns resolution."""
        session = fresh_store.create_session("external", None)

        # Agent pushes a permission request
        resp = await api_client.post(
            f"/api/sessions/{session.id}/events",
            json={
                "type": "permission_request",
                "data": {
                    "request_id": "perm_abc",
                    "tool_name": "file_write",
                    "tool_input": {"path": "/tmp/test.txt"},
                },
            },
        )
        assert resp.status_code == 200

        # Verify permission is pending
        pending = fresh_store.get_all_pending_permissions(session.id)
        assert len(pending) == 1
        assert pending[0].request_id == "perm_abc"

        # Bridge (or UI) resolves permission via /permission endpoint
        resp = await api_client.post(
            f"/api/sessions/{session.id}/permission",
            json={
                "request_id": "perm_abc",
                "allow": True,
                "message": "Approved by test user",
            },
        )
        assert resp.status_code == 200

        # Permission should no longer be pending
        pending = fresh_store.get_all_pending_permissions(session.id)
        assert len(pending) == 0

    @pytest.mark.anyio
    async def test_permission_deny(self, api_client, fresh_store: SessionStore) -> None:
        """Denied permission resolves the future with deny behavior."""
        session = fresh_store.create_session("external", None)

        await api_client.post(
            f"/api/sessions/{session.id}/events",
            json={
                "type": "permission_request",
                "data": {
                    "request_id": "perm_deny",
                    "tool_name": "shell_exec",
                    "tool_input": {"command": "rm -rf /"},
                },
            },
        )

        # Get the future before resolving
        perm = fresh_store.get_pending_permission(session.id, "perm_deny")
        future = perm.future

        resp = await api_client.post(
            f"/api/sessions/{session.id}/permission",
            json={
                "request_id": "perm_deny",
                "allow": False,
                "message": "Too dangerous",
            },
        )
        assert resp.status_code == 200

        # Future should be resolved with deny
        assert future.done()
        result = await future
        assert result["behavior"] == "deny"

    @pytest.mark.anyio
    async def test_double_resolve_fails(self, api_client, fresh_store: SessionStore) -> None:
        """Second resolution of same permission returns 404."""
        session = fresh_store.create_session("external", None)

        await api_client.post(
            f"/api/sessions/{session.id}/events",
            json={
                "type": "permission_request",
                "data": {"request_id": "perm_dup", "tool_name": "test"},
            },
        )

        # First resolve succeeds
        resp = await api_client.post(
            f"/api/sessions/{session.id}/permission",
            json={"request_id": "perm_dup", "allow": True},
        )
        assert resp.status_code == 200

        # Second resolve fails
        resp = await api_client.post(
            f"/api/sessions/{session.id}/permission",
            json={"request_id": "perm_dup", "allow": False},
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_resolve_nonexistent_permission(self, api_client, fresh_store: SessionStore) -> None:
        """Resolving a permission that was never created returns 404."""
        session = fresh_store.create_session("external", None)

        resp = await api_client.post(
            f"/api/sessions/{session.id}/permission",
            json={"request_id": "perm_ghost", "allow": True},
        )
        assert resp.status_code == 404


class TestHumanInputToAgent:
    """End-to-end: human sends input via /input, agent polls and receives it."""

    @pytest.mark.anyio
    async def test_input_appears_in_poll(self, api_client, fresh_store: SessionStore, monkeypatch) -> None:
        """Input sent via /sessions/{id}/input appears in poll results."""
        session = fresh_store.create_session("external", None)
        # Put session in AWAITING_INPUT so /input endpoint accepts it
        session.state = SessionState.AWAITING_INPUT
        fresh_store.update_session(session)

        # Mock the runner so send_input doesn't fail
        mock_runner = AsyncMock()
        mock_runner.runner_type = "mock"
        monkeypatch.setattr(
            "tether.api.sessions.get_api_runner", lambda *a, **kw: mock_runner
        )

        # Send input (as if from bridge or UI)
        resp = await api_client.post(
            f"/api/sessions/{session.id}/input",
            json={"text": "Please continue with the task"},
        )
        assert resp.status_code == 200

        # Agent polls for events
        resp = await api_client.get(
            f"/api/sessions/{session.id}/events/poll",
            params={"since_seq": 0, "types": "user_input"},
        )
        assert resp.status_code == 200
        events = resp.json()["events"]

        # Should see the input
        input_events = [e for e in events if e["type"] == "user_input"]
        assert len(input_events) >= 1
        assert input_events[0]["data"]["text"] == "Please continue with the task"

    @pytest.mark.anyio
    async def test_input_transitions_state(self, api_client, fresh_store: SessionStore, monkeypatch) -> None:
        """Input via /sessions/{id}/input transitions AWAITING_INPUT -> RUNNING."""
        session = fresh_store.create_session("external", None)
        session.state = SessionState.AWAITING_INPUT
        session.directory = "/tmp"
        fresh_store.update_session(session)

        mock_runner = AsyncMock()
        mock_runner.runner_type = "mock"
        monkeypatch.setattr(
            "tether.api.sessions.get_api_runner", lambda *a, **kw: mock_runner
        )

        await api_client.post(
            f"/api/sessions/{session.id}/input",
            json={"text": "continue"},
        )

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.RUNNING


class TestPlatformValidation:
    """Session creation with unconfigured or missing platforms."""

    @pytest.mark.anyio
    async def test_unconfigured_platform_returns_400(self, api_client, fresh_store: SessionStore) -> None:
        """Creating session with platform that has no registered bridge returns 400."""
        resp = await api_client.post(
            "/api/sessions",
            json={
                "agent_name": "Test Agent",
                "agent_type": "test",
                "session_name": "Test",
                "platform": "nonexistent_platform",
            },
        )
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_no_platform_skips_thread_creation(self, api_client, fresh_store: SessionStore) -> None:
        """Creating session without platform works and has no platform binding."""
        resp = await api_client.post(
            "/api/sessions",
            json={
                "agent_name": "Test Agent",
                "agent_type": "test",
                "session_name": "Test",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["platform"] is None
        assert data["platform_thread_id"] is None

    @pytest.mark.anyio
    async def test_session_cleaned_up_on_platform_failure(self, api_client, fresh_store: SessionStore) -> None:
        """If thread creation fails, the session is deleted (not left orphaned)."""
        resp = await api_client.post(
            "/api/sessions",
            json={
                "agent_name": "Test",
                "agent_type": "test",
                "session_name": "Test",
                "platform": "broken_platform",
            },
        )
        assert resp.status_code == 400

        # No orphaned sessions should exist
        sessions = fresh_store.list_sessions()
        platform_sessions = [s for s in sessions if s.platform == "broken_platform"]
        assert len(platform_sessions) == 0


class TestBridgeSubscriberResilience:
    """Bridge subscriber continues after bridge errors."""

    @pytest.mark.anyio
    async def test_subscriber_survives_bridge_error(self, fresh_store: SessionStore) -> None:
        """Subscriber keeps routing after bridge.on_output() throws."""
        bridge = FailingBridge()
        manager = BridgeManager()
        manager.register_bridge("failing", bridge)

        session = fresh_store.create_session("external", None)
        session.platform = "failing"
        fresh_store.update_session(session)

        subscriber = BridgeSubscriber()
        # Patch store import inside subscriber
        import tether.bridges.subscriber
        original_manager = tether.bridges.subscriber.bridge_manager
        tether.bridges.subscriber.bridge_manager = manager

        try:
            subscriber.subscribe(session.id, "failing")
            await asyncio.sleep(0.05)  # let task start

            # First event: bridge will throw on on_output
            await fresh_store.emit(session.id, {
                "session_id": session.id,
                "ts": "2026-01-01T00:00:00Z",
                "seq": fresh_store.next_seq(session.id),
                "type": "output",
                "data": {"text": "this will fail", "final": True},
            })

            # Second event: status change (different handler, should still work)
            await fresh_store.emit(session.id, {
                "session_id": session.id,
                "ts": "2026-01-01T00:00:01Z",
                "seq": fresh_store.next_seq(session.id),
                "type": "session_state",
                "data": {"state": "ERROR"},
            })

            await _wait(bridge.status_received)

            # Bridge error was hit
            assert bridge.output_call_count >= 1
            # But status event still came through
            assert len(bridge.status_calls) >= 1
            assert bridge.status_calls[0]["status"] == "error"
        finally:
            subscriber.unsubscribe(session.id)
            tether.bridges.subscriber.bridge_manager = original_manager

    @pytest.mark.anyio
    async def test_subscriber_routes_permission_to_bridge(self, fresh_store: SessionStore) -> None:
        """permission_request events reach the bridge as approval requests."""
        bridge = MockBridge()
        manager = BridgeManager()
        manager.register_bridge("mock", bridge)

        session = fresh_store.create_session("external", None)
        session.platform = "mock"
        fresh_store.update_session(session)

        subscriber = BridgeSubscriber()
        import tether.bridges.subscriber
        original_manager = tether.bridges.subscriber.bridge_manager
        tether.bridges.subscriber.bridge_manager = manager

        try:
            subscriber.subscribe(session.id, "mock")
            await asyncio.sleep(0.05)

            await fresh_store.emit(session.id, {
                "session_id": session.id,
                "ts": "2026-01-01T00:00:00Z",
                "seq": fresh_store.next_seq(session.id),
                "type": "permission_request",
                "data": {
                    "request_id": "perm_sub",
                    "tool_name": "file_write",
                    "tool_input": {"path": "/tmp/test"},
                },
            })

            await _wait(bridge.approval_received)

            assert len(bridge.approval_calls) == 1
            req = bridge.approval_calls[0]["request"]
            assert req.request_id == "perm_sub"
            assert "file_write" in req.title
            assert "Allow" in req.options
            assert "Deny" in req.options
        finally:
            subscriber.unsubscribe(session.id)
            tether.bridges.subscriber.bridge_manager = original_manager

    @pytest.mark.anyio
    async def test_subscriber_skips_history_events(self, fresh_store: SessionStore) -> None:
        """Events with is_history flag are not routed to bridges."""
        bridge = MockBridge()
        manager = BridgeManager()
        manager.register_bridge("mock", bridge)

        session = fresh_store.create_session("external", None)
        session.platform = "mock"
        fresh_store.update_session(session)

        subscriber = BridgeSubscriber()
        import tether.bridges.subscriber
        original_manager = tether.bridges.subscriber.bridge_manager
        tether.bridges.subscriber.bridge_manager = manager

        try:
            subscriber.subscribe(session.id, "mock")
            await asyncio.sleep(0.05)

            # Emit history event (should be skipped)
            await fresh_store.emit(session.id, {
                "session_id": session.id,
                "ts": "2026-01-01T00:00:00Z",
                "seq": fresh_store.next_seq(session.id),
                "type": "output",
                "data": {"text": "old history", "final": True, "is_history": True},
            })

            # Emit real event
            await fresh_store.emit(session.id, {
                "session_id": session.id,
                "ts": "2026-01-01T00:00:01Z",
                "seq": fresh_store.next_seq(session.id),
                "type": "output",
                "data": {"text": "new output", "final": True},
            })

            await _wait(bridge.output_received)

            texts = [c["text"] for c in bridge.output_calls]
            assert "new output" in texts
            assert "old history" not in texts
        finally:
            subscriber.unsubscribe(session.id)
            tether.bridges.subscriber.bridge_manager = original_manager


class TestStateTransitionsExternalAgent:
    """State machine enforcement for external agent event pushes."""

    @pytest.mark.anyio
    async def test_auto_transition_created_to_running(self, api_client, fresh_store: SessionStore) -> None:
        """First event auto-transitions CREATED -> RUNNING."""
        session = fresh_store.create_session("external", None)
        assert session.state == SessionState.CREATED

        await api_client.post(
            f"/api/sessions/{session.id}/events",
            json={"type": "output", "data": {"text": "hello"}},
        )

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.RUNNING

    @pytest.mark.anyio
    async def test_status_done_transitions_to_awaiting_input(self, api_client, fresh_store: SessionStore) -> None:
        """Status 'done' transitions RUNNING -> AWAITING_INPUT."""
        session = fresh_store.create_session("external", None)

        # Get to RUNNING first
        await api_client.post(
            f"/api/sessions/{session.id}/events",
            json={"type": "output", "data": {"text": "working..."}},
        )

        # Signal done
        await api_client.post(
            f"/api/sessions/{session.id}/events",
            json={"type": "status", "data": {"status": "done"}},
        )

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.AWAITING_INPUT

    @pytest.mark.anyio
    async def test_error_event_transitions_to_error(self, api_client, fresh_store: SessionStore) -> None:
        """Error event transitions to ERROR state."""
        session = fresh_store.create_session("external", None)

        # Get to RUNNING first
        await api_client.post(
            f"/api/sessions/{session.id}/events",
            json={"type": "output", "data": {"text": "starting"}},
        )

        # Push error
        resp = await api_client.post(
            f"/api/sessions/{session.id}/events",
            json={
                "type": "error",
                "data": {"code": "CRASH", "message": "Segfault"},
            },
        )
        assert resp.status_code == 200

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.ERROR

    @pytest.mark.anyio
    async def test_push_to_nonexistent_session(self, api_client, fresh_store: SessionStore) -> None:
        """Pushing events to a non-existent session returns 404."""
        resp = await api_client.post(
            "/api/sessions/sess_does_not_exist/events",
            json={"type": "output", "data": {"text": "hello"}},
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_invalid_event_type_rejected(self, api_client, fresh_store: SessionStore) -> None:
        """Pushing an unknown event type returns 422 (validation error)."""
        session = fresh_store.create_session("external", None)

        resp = await api_client.post(
            f"/api/sessions/{session.id}/events",
            json={"type": "invalid_type", "data": {}},
        )
        assert resp.status_code == 422


class TestPollFiltering:
    """Event polling returns correct types and respects seq filtering."""

    @pytest.mark.anyio
    async def test_poll_default_filters(self, api_client, fresh_store: SessionStore) -> None:
        """Default poll filters return user_input and permission_resolved only."""
        session = fresh_store.create_session("external", None)

        # Emit various event types
        for event_type in ("output", "session_state", "user_input", "error"):
            await fresh_store.emit(session.id, {
                "session_id": session.id,
                "ts": "2026-01-01T00:00:00Z",
                "seq": fresh_store.next_seq(session.id),
                "type": event_type,
                "data": {"text": f"test {event_type}"},
            })

        resp = await api_client.get(
            f"/api/sessions/{session.id}/events/poll",
            params={"since_seq": 0},
        )
        assert resp.status_code == 200
        events = resp.json()["events"]

        types = {e["type"] for e in events}
        assert "user_input" in types
        assert "output" not in types
        assert "session_state" not in types
        assert "error" not in types

    @pytest.mark.anyio
    async def test_poll_custom_type_filter(self, api_client, fresh_store: SessionStore) -> None:
        """Custom type filter via query parameter works."""
        session = fresh_store.create_session("external", None)

        await fresh_store.emit(session.id, {
            "session_id": session.id,
            "ts": "2026-01-01T00:00:00Z",
            "seq": fresh_store.next_seq(session.id),
            "type": "error",
            "data": {"code": "ERR", "message": "fail"},
        })

        resp = await api_client.get(
            f"/api/sessions/{session.id}/events/poll",
            params={"since_seq": 0, "types": "error"},
        )
        events = resp.json()["events"]
        assert len(events) == 1
        assert events[0]["type"] == "error"

    @pytest.mark.anyio
    async def test_poll_since_seq_excludes_old(self, api_client, fresh_store: SessionStore) -> None:
        """since_seq parameter excludes older events."""
        session = fresh_store.create_session("external", None)

        # Emit 3 user_input events
        seqs = []
        for i in range(3):
            seq = fresh_store.next_seq(session.id)
            seqs.append(seq)
            await fresh_store.emit(session.id, {
                "session_id": session.id,
                "ts": "2026-01-01T00:00:00Z",
                "seq": seq,
                "type": "user_input",
                "data": {"text": f"msg {i}"},
            })

        # Poll from after first event
        resp = await api_client.get(
            f"/api/sessions/{session.id}/events/poll",
            params={"since_seq": seqs[0]},
        )
        events = resp.json()["events"]

        texts = [e["data"]["text"] for e in events]
        assert "msg 0" not in texts
        assert "msg 1" in texts
        assert "msg 2" in texts
