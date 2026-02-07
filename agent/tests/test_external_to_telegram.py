"""Tests for external agent to bridge integration via converged API."""

import asyncio

import pytest

from tether.bridges.manager import bridge_manager
from tether.models import SessionState
from tether.store import SessionStore


class MockTelegramBridge:
    """Mock Telegram bridge for testing."""

    def __init__(self):
        self.output_calls = []
        self.approval_calls = []
        self.status_calls = []
        self.thread_calls = []

    async def on_output(self, session_id: str, text: str, metadata: dict | None = None) -> None:
        self.output_calls.append({"session_id": session_id, "text": text, "metadata": metadata})

    async def on_approval_request(self, session_id: str, request) -> None:
        self.approval_calls.append({"session_id": session_id, "request": request})

    async def on_status_change(self, session_id: str, status: str, metadata: dict | None = None) -> None:
        self.status_calls.append({"session_id": session_id, "status": status, "metadata": metadata})

    async def create_thread(self, session_id: str, session_name: str) -> dict:
        self.thread_calls.append({"session_id": session_id, "session_name": session_name})
        return {"thread_id": f"mock_{session_id}", "platform": "telegram"}


class TestExternalAgentToTelegramIntegration:
    """Test that external agent events are routed to Telegram via bridge subscriber."""

    @pytest.mark.anyio
    async def test_session_creates_telegram_thread(self, api_client, fresh_store: SessionStore) -> None:
        """Creating a session with platform=telegram auto-creates a thread."""
        mock_bridge = MockTelegramBridge()
        bridge_manager.register_bridge("telegram", mock_bridge)

        response = await api_client.post(
            "/api/sessions",
            json={
                "agent_name": "Test Agent",
                "agent_type": "test",
                "session_name": "Test Session",
                "platform": "telegram",
            },
        )

        assert response.status_code == 201
        data = response.json()

        # Verify thread was created
        assert len(mock_bridge.thread_calls) == 1
        assert mock_bridge.thread_calls[0]["session_name"] == "Test Session"

    @pytest.mark.anyio
    async def test_output_routes_to_telegram_via_subscriber(self, api_client, fresh_store: SessionStore) -> None:
        """Output events pushed via /events reach the bridge via subscriber."""
        mock_bridge = MockTelegramBridge()
        bridge_manager.register_bridge("telegram", mock_bridge)

        # Create session with platform binding
        response = await api_client.post(
            "/api/sessions",
            json={
                "agent_name": "Test",
                "agent_type": "test",
                "session_name": "Test",
                "platform": "telegram",
            },
        )
        session_id = response.json()["id"]

        # Push output event through store.emit via /events endpoint
        await api_client.post(
            f"/api/sessions/{session_id}/events",
            json={
                "type": "output",
                "data": {"text": "Hello from external agent!", "is_final": True},
            },
        )

        # Give subscriber task time to process
        await asyncio.sleep(0.1)

        # Verify output was routed to Telegram via subscriber
        assert len(mock_bridge.output_calls) >= 1
        texts = [c["text"] for c in mock_bridge.output_calls]
        assert "Hello from external agent!" in texts

    @pytest.mark.anyio
    async def test_status_routes_to_telegram_via_subscriber(self, api_client, fresh_store: SessionStore) -> None:
        """Status changes reach the bridge via subscriber."""
        mock_bridge = MockTelegramBridge()
        bridge_manager.register_bridge("telegram", mock_bridge)

        # Create session with platform
        response = await api_client.post(
            "/api/sessions",
            json={
                "agent_name": "Test",
                "agent_type": "test",
                "session_name": "Test",
                "platform": "telegram",
            },
        )
        session_id = response.json()["id"]

        # Push output to trigger CREATED -> RUNNING, then push error status
        await api_client.post(
            f"/api/sessions/{session_id}/events",
            json={"type": "output", "data": {"text": "start"}},
        )
        await api_client.post(
            f"/api/sessions/{session_id}/events",
            json={"type": "status", "data": {"status": "error"}},
        )

        # Give subscriber time
        await asyncio.sleep(0.1)

        # Should have received status updates via subscriber
        assert len(mock_bridge.status_calls) >= 1
