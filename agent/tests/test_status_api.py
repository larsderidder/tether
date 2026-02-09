"""Tests for status API endpoints."""

import pytest
import httpx

from tether.models import SessionState
from tether.store import SessionStore


class TestBridgeStatusEndpoint:
    """Test /api/status/bridges endpoint."""

    @pytest.mark.anyio
    async def test_get_bridge_status_no_bridges(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Test bridge status with no bridges registered."""
        response = await api_client.get("/api/status/bridges")
        assert response.status_code == 200
        data = response.json()
        assert "bridges" in data
        bridges = data["bridges"]
        assert len(bridges) == 3  # telegram, slack, discord
        for bridge in bridges:
            assert bridge["status"] == "not_configured"
            assert bridge["platform"] in ["telegram", "slack", "discord"]


class TestSessionStatsEndpoint:
    """Test /api/status/sessions endpoint."""

    @pytest.mark.anyio
    async def test_get_session_stats_empty(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Test session stats with no sessions."""
        response = await api_client.get("/api/status/sessions")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["by_state"] == {}
        assert data["by_platform"] == {}
        assert data["recent_activity"] == []

    @pytest.mark.anyio
    async def test_get_session_stats_with_sessions(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore
    ) -> None:
        """Test session stats with various sessions."""
        # Create sessions in different states
        sess1 = fresh_store.create_session("test-repo-1", None)
        sess1.state = SessionState.RUNNING
        sess1.platform = "telegram"
        fresh_store.update_session(sess1)

        sess2 = fresh_store.create_session("test-repo-2", None)
        sess2.state = SessionState.AWAITING_INPUT
        sess2.platform = "slack"
        fresh_store.update_session(sess2)

        sess3 = fresh_store.create_session("test-repo-3", None)
        sess3.state = SessionState.ERROR
        fresh_store.update_session(sess3)

        sess4 = fresh_store.create_session("test-repo-4", None)
        sess4.state = SessionState.RUNNING
        sess4.platform = "telegram"
        fresh_store.update_session(sess4)

        response = await api_client.get("/api/status/sessions")
        assert response.status_code == 200
        data = response.json()

        # Check totals
        assert data["total"] == 4

        # Check state breakdown
        assert data["by_state"]["RUNNING"] == 2
        assert data["by_state"]["AWAITING_INPUT"] == 1
        assert data["by_state"]["ERROR"] == 1

        # Check platform breakdown
        assert data["by_platform"]["telegram"] == 2
        assert data["by_platform"]["slack"] == 1
        assert data["by_platform"]["none"] == 1

        # Check recent activity
        assert len(data["recent_activity"]) == 4
        for activity in data["recent_activity"]:
            assert "session_id" in activity
            assert "name" in activity
            assert "state" in activity
            assert "last_activity_at" in activity
            assert "message_count" in activity

    @pytest.mark.anyio
    async def test_get_session_stats_recent_activity_limit(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore
    ) -> None:
        """Test that recent activity is limited to 10 sessions."""
        # Create 15 sessions
        for i in range(15):
            sess = fresh_store.create_session(f"test-repo-{i}", None)
            sess.state = SessionState.CREATED
            fresh_store.update_session(sess)

        response = await api_client.get("/api/status/sessions")
        assert response.status_code == 200
        data = response.json()

        assert data["total"] == 15
        # Recent activity should be limited to 10
        assert len(data["recent_activity"]) <= 10

    @pytest.mark.anyio
    async def test_get_session_stats_with_platform_bindings(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore
    ) -> None:
        """Test session stats correctly aggregates platform bindings."""
        # Create sessions with different platforms
        for platform in ["telegram", "slack", "discord", None]:
            sess = fresh_store.create_session(f"test-repo-{platform}", None)
            sess.platform = platform
            fresh_store.update_session(sess)

        response = await api_client.get("/api/status/sessions")
        assert response.status_code == 200
        data = response.json()

        assert data["total"] == 4
        assert data["by_platform"]["telegram"] == 1
        assert data["by_platform"]["slack"] == 1
        assert data["by_platform"]["discord"] == 1
        assert data["by_platform"]["none"] == 1
