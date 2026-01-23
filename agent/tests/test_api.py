"""Tests for API endpoints."""

import pytest
import httpx

from tether.main import app
from tether.models import SessionState
from tether.store import SessionStore


@pytest.fixture
async def api_client(fresh_store) -> httpx.AsyncClient:
    """Create an async HTTP client that uses the patched store."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestHealthEndpoint:
    """Test /api/health endpoint."""

    @pytest.mark.anyio
    async def test_health_returns_ok(self, api_client: httpx.AsyncClient) -> None:
        """Health check returns ok status."""
        response = await api_client.get("/api/health")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "version" in data


class TestSessionsEndpoints:
    """Test /api/sessions endpoints."""

    @pytest.mark.anyio
    async def test_list_sessions_empty(self, api_client: httpx.AsyncClient) -> None:
        """List sessions returns empty list initially."""
        response = await api_client.get("/api/sessions")

        assert response.status_code == 200
        data = response.json()
        assert data["sessions"] == []

    @pytest.mark.anyio
    async def test_create_session(self, api_client: httpx.AsyncClient) -> None:
        """Create session returns new session in CREATED state."""
        response = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"}
        )

        assert response.status_code == 201
        data = response.json()
        session = data["session"]
        assert session["id"].startswith("sess_")
        assert session["state"] == "CREATED"
        assert session["created_at"] is not None

    @pytest.mark.anyio
    async def test_create_session_with_directory(
        self, api_client: httpx.AsyncClient, tmp_path
    ) -> None:
        """Create session with directory sets directory fields."""
        test_dir = tmp_path / "test_repo"
        test_dir.mkdir()

        response = await api_client.post(
            "/api/sessions",
            json={"directory": str(test_dir)}
        )

        assert response.status_code == 201
        data = response.json()
        session = data["session"]
        assert session["directory"] == str(test_dir)

    @pytest.mark.anyio
    async def test_create_session_invalid_directory(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Create session with nonexistent directory fails."""
        response = await api_client.post(
            "/api/sessions",
            json={"directory": "/nonexistent/path"}
        )

        assert response.status_code == 422

    @pytest.mark.anyio
    async def test_get_session(self, api_client: httpx.AsyncClient) -> None:
        """Get session returns session details."""
        # Create a session first
        create_resp = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"}
        )
        session_id = create_resp.json()["session"]["id"]

        # Get the session
        response = await api_client.get(f"/api/sessions/{session_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["id"] == session_id

    @pytest.mark.anyio
    async def test_get_nonexistent_session(self, api_client: httpx.AsyncClient) -> None:
        """Get nonexistent session returns 404."""
        response = await api_client.get("/api/sessions/nonexistent_id")

        assert response.status_code == 404

    @pytest.mark.anyio
    async def test_delete_session(self, api_client: httpx.AsyncClient) -> None:
        """Delete session removes it."""
        # Create a session
        create_resp = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"}
        )
        session_id = create_resp.json()["session"]["id"]

        # Delete it
        response = await api_client.delete(f"/api/sessions/{session_id}")
        assert response.status_code == 200

        # Verify it's gone
        get_resp = await api_client.get(f"/api/sessions/{session_id}")
        assert get_resp.status_code == 404

    @pytest.mark.anyio
    async def test_list_sessions_after_create(self, api_client: httpx.AsyncClient) -> None:
        """List sessions includes created sessions."""
        # Create two sessions
        await api_client.post("/api/sessions", json={"repo_id": "repo_1"})
        await api_client.post("/api/sessions", json={"repo_id": "repo_2"})

        response = await api_client.get("/api/sessions")

        assert response.status_code == 200
        data = response.json()
        assert len(data["sessions"]) == 2


class TestSessionLifecycle:
    """Test session start/stop/input endpoints."""

    @pytest.mark.anyio
    async def test_start_session_without_directory_fails(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Starting session without directory returns error."""
        # Create session without directory
        create_resp = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"}
        )
        session_id = create_resp.json()["session"]["id"]

        # Try to start it
        response = await api_client.post(
            f"/api/sessions/{session_id}/start",
            json={"prompt": "test prompt"}
        )

        assert response.status_code == 422

    @pytest.mark.anyio
    async def test_stop_created_session_fails(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Stopping a CREATED session returns error."""
        create_resp = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"}
        )
        session_id = create_resp.json()["session"]["id"]

        response = await api_client.post(f"/api/sessions/{session_id}/stop")

        assert response.status_code == 409

    @pytest.mark.anyio
    async def test_send_input_to_created_session_fails(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Sending input to CREATED session returns error."""
        create_resp = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"}
        )
        session_id = create_resp.json()["session"]["id"]

        response = await api_client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "some input"}
        )

        assert response.status_code == 409

    @pytest.mark.anyio
    async def test_send_input_empty_text_fails(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore, tmp_path
    ) -> None:
        """Sending empty input returns error."""
        # Create and manually set to RUNNING for this test
        test_dir = tmp_path / "test_repo"
        test_dir.mkdir()

        create_resp = await api_client.post(
            "/api/sessions",
            json={"directory": str(test_dir)}
        )
        session_id = create_resp.json()["session"]["id"]

        # Manually transition to RUNNING
        session = fresh_store.get_session(session_id)
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        response = await api_client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": ""}
        )

        assert response.status_code == 422


class TestSessionRename:
    """Test session rename endpoint."""

    @pytest.mark.anyio
    async def test_rename_session(self, api_client: httpx.AsyncClient) -> None:
        """Renaming session updates the name."""
        create_resp = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"}
        )
        session_id = create_resp.json()["session"]["id"]

        response = await api_client.patch(
            f"/api/sessions/{session_id}/rename",
            json={"name": "New Session Name"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["name"] == "New Session Name"

    @pytest.mark.anyio
    async def test_rename_nonexistent_session(self, api_client: httpx.AsyncClient) -> None:
        """Renaming nonexistent session returns 404."""
        response = await api_client.patch(
            "/api/sessions/nonexistent_id/rename",
            json={"name": "New Name"}
        )

        assert response.status_code == 404

    @pytest.mark.anyio
    async def test_rename_empty_name_fails(self, api_client: httpx.AsyncClient) -> None:
        """Renaming with empty name returns error."""
        create_resp = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"}
        )
        session_id = create_resp.json()["session"]["id"]

        response = await api_client.patch(
            f"/api/sessions/{session_id}/rename",
            json={"name": ""}
        )

        assert response.status_code == 422
