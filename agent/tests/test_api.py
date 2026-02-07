"""Tests for API endpoints."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from tether.models import SessionState
from tether.api.runner_events import ApiRunnerEvents
from tether.api.state import _session_locks
from tether.store import SessionStore


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
        assert data == []

    @pytest.mark.anyio
    async def test_create_session(self, api_client: httpx.AsyncClient) -> None:
        """Create session returns new session in CREATED state."""
        response = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"}
        )

        assert response.status_code == 201
        session = response.json()
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
        session = response.json()
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
        session_id = create_resp.json()["id"]

        # Get the session
        response = await api_client.get(f"/api/sessions/{session_id}")

        assert response.status_code == 200
        session = response.json()
        assert session["id"] == session_id

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
        session_id = create_resp.json()["id"]

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
        sessions = response.json()
        assert len(sessions) == 2


class TestSessionLifecycle:
    """Test session start/interrupt/input endpoints."""

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
        session_id = create_resp.json()["id"]

        # Try to start it
        response = await api_client.post(
            f"/api/sessions/{session_id}/start",
            json={"prompt": "test prompt"}
        )

        assert response.status_code == 422

    @pytest.mark.anyio
    async def test_interrupt_created_session_fails(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Interrupting a CREATED session returns error."""
        create_resp = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"}
        )
        session_id = create_resp.json()["id"]

        response = await api_client.post(f"/api/sessions/{session_id}/interrupt")

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
        session_id = create_resp.json()["id"]

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
        session_id = create_resp.json()["id"]

        # Manually transition to RUNNING
        session = fresh_store.get_session(session_id)
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        response = await api_client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": ""}
        )

        assert response.status_code == 422

    @pytest.mark.anyio
    async def test_input_emits_output_and_input_required(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore, tmp_path, monkeypatch
    ) -> None:
        """Input should emit output_final and input_required when runner completes."""
        test_dir = tmp_path / "test_repo"
        test_dir.mkdir()

        create_resp = await api_client.post(
            "/api/sessions",
            json={"directory": str(test_dir)}
        )
        session_id = create_resp.json()["id"]

        session = fresh_store.get_session(session_id)
        session.state = SessionState.AWAITING_INPUT
        session.directory = str(test_dir)
        fresh_store.update_session(session)

        events = ApiRunnerEvents()

        class FakeRunner:
            runner_type = "fake"

            async def send_input(self, session_id: str, text: str) -> None:
                await events.on_output(
                    session_id,
                    "combined",
                    "ok",
                    kind="final",
                    is_final=True,
                )
                await events.on_awaiting_input(session_id)

        fake_runner = FakeRunner()
        monkeypatch.setattr("tether.api.sessions.get_api_runner", lambda *args, **kwargs: fake_runner)

        response = await api_client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "hello"}
        )

        assert response.status_code == 200

        events_log = fresh_store.read_event_log(session_id, since_seq=0)
        types = [event["type"] for event in events_log]
        assert "output_final" in types
        assert "input_required" in types

        session = fresh_store.get_session(session_id)
        assert session.state == SessionState.AWAITING_INPUT


class TestSessionRename:
    """Test session rename endpoint."""

    @pytest.mark.anyio
    async def test_rename_session(self, api_client: httpx.AsyncClient) -> None:
        """Renaming session updates the name."""
        create_resp = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"}
        )
        session_id = create_resp.json()["id"]

        response = await api_client.patch(
            f"/api/sessions/{session_id}/rename",
            json={"name": "New Session Name"}
        )

        assert response.status_code == 200
        session = response.json()
        assert session["name"] == "New Session Name"

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
        session_id = create_resp.json()["id"]

        response = await api_client.patch(
            f"/api/sessions/{session_id}/rename",
            json={"name": ""}
        )

        assert response.status_code == 422

    @pytest.mark.anyio
    async def test_create_session_with_adapter(
        self, api_client: httpx.AsyncClient, tmp_path
    ) -> None:
        """Create session with adapter field stores it correctly."""
        test_dir = tmp_path / "test_repo"
        test_dir.mkdir()

        with patch("tether.api.runner_registry.get_runner") as mock_get_runner:
            mock_runner = MagicMock()
            mock_runner.runner_type = "claude_api"
            mock_get_runner.return_value = mock_runner

            response = await api_client.post(
                "/api/sessions",
                json={"directory": str(test_dir), "adapter": "claude_api"}
            )

            assert response.status_code == 201
            session = response.json()
            assert session["adapter"] == "claude_api"

    @pytest.mark.anyio
    async def test_create_session_without_adapter(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Create session without adapter uses default."""
        response = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"}
        )

        assert response.status_code == 201
        session = response.json()
        assert session["adapter"] is None  # Default adapter

    @pytest.mark.anyio
    async def test_create_session_invalid_adapter(
        self, api_client: httpx.AsyncClient, tmp_path
    ) -> None:
        """Create session with invalid adapter returns 422."""
        test_dir = tmp_path / "test_repo"
        test_dir.mkdir()

        with patch("tether.api.runner_registry.get_runner") as mock_get_runner:
            mock_get_runner.side_effect = ValueError("Unknown agent adapter: invalid")

            response = await api_client.post(
                "/api/sessions",
                json={"directory": str(test_dir), "adapter": "invalid"}
            )

            assert response.status_code == 422
            data = response.json()
            assert "Invalid adapter" in data["error"]["message"]

    @pytest.mark.anyio
    async def test_session_response_includes_adapter(
        self, api_client: httpx.AsyncClient, tmp_path
    ) -> None:
        """Session response includes adapter field."""
        test_dir = tmp_path / "test_repo"
        test_dir.mkdir()

        with patch("tether.api.runner_registry.get_runner") as mock_get_runner:
            mock_runner = MagicMock()
            mock_runner.runner_type = "codex"
            mock_get_runner.return_value = mock_runner

            create_resp = await api_client.post(
                "/api/sessions",
                json={"directory": str(test_dir), "adapter": "codex_sdk_sidecar"}
            )
            session_id = create_resp.json()["id"]

            # Get session and verify adapter is present
            get_resp = await api_client.get(f"/api/sessions/{session_id}")
            assert get_resp.status_code == 200
            session = get_resp.json()
            assert "adapter" in session
            assert session["adapter"] == "codex_sdk_sidecar"


class TestDeleteSessionCleanup:
    """Test that deleting a session cleans up associated resources."""

    @pytest.mark.anyio
    async def test_delete_clears_pending_permissions(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore
    ) -> None:
        """Deleting a session resolves pending permission futures."""
        create_resp = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"},
        )
        session_id = create_resp.json()["id"]

        # Add a pending permission with a future
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        fresh_store.add_pending_permission(
            session_id, "perm_1", "Bash", {"command": "rm -rf /"}, future
        )

        # Delete the session
        response = await api_client.delete(f"/api/sessions/{session_id}")
        assert response.status_code == 200

        # Future should be cancelled (not hanging)
        assert future.done()

    @pytest.mark.anyio
    async def test_delete_unsubscribes_bridge(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore, monkeypatch
    ) -> None:
        """Deleting a session with platform binding cancels bridge subscriber."""
        create_resp = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"},
        )
        session_id = create_resp.json()["id"]

        # Manually set platform on the session
        session = fresh_store.get_session(session_id)
        session.platform = "telegram"
        fresh_store.update_session(session)

        # Track unsubscribe calls on the singleton
        from tether.bridges.subscriber import bridge_subscriber

        mock_unsubscribe = MagicMock()
        monkeypatch.setattr(bridge_subscriber, "unsubscribe", mock_unsubscribe)

        response = await api_client.delete(f"/api/sessions/{session_id}")
        assert response.status_code == 200

        mock_unsubscribe.assert_called_once_with(session_id)

    @pytest.mark.anyio
    async def test_delete_without_platform_skips_unsubscribe(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Deleting a session without platform doesn't try to unsubscribe."""
        create_resp = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"},
        )
        session_id = create_resp.json()["id"]

        # Should not raise even though no bridge subscriber exists
        response = await api_client.delete(f"/api/sessions/{session_id}")
        assert response.status_code == 200


class TestStartSessionPersistsApprovalMode:
    """Test that start_session persists approval_choice to session.approval_mode."""

    @pytest.mark.anyio
    async def test_start_persists_approval_choice(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore, tmp_path, monkeypatch
    ) -> None:
        """Starting a session persists the approval_choice to session.approval_mode."""
        test_dir = tmp_path / "test_repo"
        test_dir.mkdir()

        create_resp = await api_client.post(
            "/api/sessions",
            json={"directory": str(test_dir)},
        )
        session_id = create_resp.json()["id"]

        # Mock runner to avoid actually launching anything
        mock_runner = MagicMock()
        mock_runner.runner_type = "fake"
        mock_runner.start = AsyncMock()
        monkeypatch.setattr(
            "tether.api.sessions.get_api_runner",
            lambda *a, **kw: mock_runner,
        )

        response = await api_client.post(
            f"/api/sessions/{session_id}/start",
            json={"prompt": "hello", "approval_choice": 1},
        )
        assert response.status_code == 200

        session = fresh_store.get_session(session_id)
        assert session.approval_mode == 1


class TestConcurrentStartPrevention:
    """Test that concurrent start/input requests are serialized."""

    @pytest.mark.anyio
    async def test_concurrent_starts_only_one_succeeds(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore, tmp_path, monkeypatch
    ) -> None:
        """Two concurrent starts on the same session — only one transitions to RUNNING."""
        test_dir = tmp_path / "test_repo"
        test_dir.mkdir()

        create_resp = await api_client.post(
            "/api/sessions",
            json={"directory": str(test_dir)},
        )
        session_id = create_resp.json()["id"]

        start_count = 0
        start_event = asyncio.Event()

        async def slow_start(sid, prompt, approval_choice):
            nonlocal start_count
            start_count += 1
            # Simulate a slow runner.start() that yields control
            await start_event.wait()

        mock_runner = MagicMock()
        mock_runner.runner_type = "fake"
        mock_runner.start = slow_start
        monkeypatch.setattr(
            "tether.api.sessions.get_api_runner",
            lambda *a, **kw: mock_runner,
        )

        # Launch two concurrent starts
        task1 = asyncio.create_task(
            api_client.post(
                f"/api/sessions/{session_id}/start",
                json={"prompt": "hello1", "approval_choice": 0},
            )
        )
        task2 = asyncio.create_task(
            api_client.post(
                f"/api/sessions/{session_id}/start",
                json={"prompt": "hello2", "approval_choice": 0},
            )
        )

        # Let the first start proceed
        await asyncio.sleep(0.01)
        start_event.set()

        resp1, resp2 = await asyncio.gather(task1, task2)
        statuses = sorted([resp1.status_code, resp2.status_code])

        # One should succeed (200), the other should get 409 (already RUNNING)
        assert statuses == [200, 409]
        # Runner should only be started once
        assert start_count == 1

    @pytest.mark.anyio
    async def test_delete_cleans_up_lock(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Deleting a session removes its lock from the registry."""
        create_resp = await api_client.post(
            "/api/sessions",
            json={"repo_id": "test_repo"},
        )
        session_id = create_resp.json()["id"]

        # Access the session (to trigger lock creation if accessed)
        _session_locks.pop(session_id, None)  # clean slate
        from tether.api.state import session_lock
        _ = session_lock(session_id)
        assert session_id in _session_locks

        response = await api_client.delete(f"/api/sessions/{session_id}")
        assert response.status_code == 200
        assert session_id not in _session_locks
