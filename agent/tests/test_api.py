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
            mock_runner.runner_type = "claude-subprocess"
            mock_get_runner.return_value = mock_runner

            response = await api_client.post(
                "/api/sessions",
                json={"directory": str(test_dir), "adapter": "claude_subprocess"}
            )

            assert response.status_code == 201
            session = response.json()
            assert session["adapter"] == "claude_subprocess"

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

        mock_unsubscribe = AsyncMock()
        monkeypatch.setattr(bridge_subscriber, "unsubscribe", mock_unsubscribe)

        response = await api_client.delete(f"/api/sessions/{session_id}")
        assert response.status_code == 200

        mock_unsubscribe.assert_called_once_with(session_id, platform="telegram")

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


class TestCreateSessionClone:
    """Tests for clone_url-based session creation."""

    def _make_workspace_result(self, path: str):
        """Return a WorkspaceResult pointing at *path*."""
        from tether.workspace import WorkspaceResult
        return WorkspaceResult(path=path, is_worktree=True, repo_hash="a1b2c3d4")

    @pytest.mark.anyio
    async def test_create_session_with_clone_url(
        self, api_client: httpx.AsyncClient, tmp_path, monkeypatch
    ) -> None:
        """Creating a session with clone_url creates a workspace and sets directory."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

        cloned_path = str(tmp_path / "cloned")
        import os
        os.makedirs(cloned_path)

        with patch("tether.api.sessions._workspace.create_workspace",
                   return_value=self._make_workspace_result(cloned_path)) as mock_cw:
            response = await api_client.post(
                "/api/sessions",
                json={"clone_url": "https://github.com/owner/repo.git"},
            )

        assert response.status_code == 201
        session = response.json()
        assert session["state"] == "CREATED"
        assert session["directory"] == cloned_path
        assert session["clone_url"] == "https://github.com/owner/repo.git"
        mock_cw.assert_called_once()

    @pytest.mark.anyio
    async def test_create_session_with_clone_url_and_branch(
        self, api_client: httpx.AsyncClient, tmp_path, monkeypatch
    ) -> None:
        """clone_branch is forwarded to create_workspace."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

        cloned_path = str(tmp_path / "cloned")
        import os
        os.makedirs(cloned_path)

        with patch("tether.api.sessions._workspace.create_workspace",
                   return_value=self._make_workspace_result(cloned_path)) as mock_cw:
            response = await api_client.post(
                "/api/sessions",
                json={
                    "clone_url": "https://github.com/owner/repo.git",
                    "clone_branch": "feature",
                },
            )

        assert response.status_code == 201
        call_kwargs = mock_cw.call_args
        assert call_kwargs.kwargs.get("branch") == "feature"

    @pytest.mark.anyio
    async def test_clone_url_and_directory_mutually_exclusive(
        self, api_client: httpx.AsyncClient, tmp_path
    ) -> None:
        """Providing both clone_url and directory returns 422."""
        test_dir = tmp_path / "repo"
        test_dir.mkdir()

        response = await api_client.post(
            "/api/sessions",
            json={
                "clone_url": "https://github.com/owner/repo.git",
                "directory": str(test_dir),
            },
        )

        assert response.status_code == 422

    @pytest.mark.anyio
    async def test_invalid_clone_url_returns_422(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """A non-git URL is rejected with 422."""
        response = await api_client.post(
            "/api/sessions",
            json={"clone_url": "not-a-git-url"},
        )

        assert response.status_code == 422

    @pytest.mark.anyio
    async def test_clone_failure_returns_error_and_cleans_up(
        self, api_client: httpx.AsyncClient, tmp_path, monkeypatch, fresh_store
    ) -> None:
        """A workspace error returns 422 and the session is deleted."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

        from tether.workspace import WorkspaceError

        with patch("tether.api.sessions._workspace.create_workspace",
                   side_effect=WorkspaceError("clone failed")):
            response = await api_client.post(
                "/api/sessions",
                json={"clone_url": "https://github.com/owner/repo.git"},
            )

        assert response.status_code == 422
        # Session must have been cleaned up
        assert fresh_store.list_sessions() == []

    @pytest.mark.anyio
    async def test_clone_url_sets_clone_url_in_response(
        self, api_client: httpx.AsyncClient, tmp_path, monkeypatch
    ) -> None:
        """SessionResponse.clone_url is populated when repo_ref_type is url."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

        import os
        cloned_path = str(tmp_path / "cloned")
        os.makedirs(cloned_path)

        url = "git@github.com:owner/repo.git"
        with patch("tether.api.sessions._workspace.create_workspace",
                   return_value=self._make_workspace_result(cloned_path)):
            response = await api_client.post(
                "/api/sessions",
                json={"clone_url": url},
            )

        assert response.status_code == 201
        assert response.json()["clone_url"] == url

    @pytest.mark.anyio
    async def test_force_clone_uses_clone_repo(
        self, api_client: httpx.AsyncClient, tmp_path, monkeypatch
    ) -> None:
        """force_clone=True bypasses create_workspace and calls clone_repo directly."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

        import os
        cloned_path = str(tmp_path / "cloned")
        os.makedirs(cloned_path)

        with patch("tether.api.sessions._workspace.clone_repo", return_value=cloned_path) as mock_clone, \
             patch("tether.api.sessions._workspace.workspace_path", return_value=cloned_path), \
             patch("tether.api.sessions._workspace.create_workspace") as mock_cw:
            response = await api_client.post(
                "/api/sessions",
                json={"clone_url": "https://github.com/owner/repo.git", "force_clone": True},
            )

        assert response.status_code == 201
        mock_clone.assert_called_once()
        mock_cw.assert_not_called()

    @pytest.mark.anyio
    async def test_force_clone_error_returns_422(
        self, api_client: httpx.AsyncClient, tmp_path, monkeypatch, fresh_store
    ) -> None:
        """force_clone=True: a clone_repo failure still returns 422 and cleans up."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

        from tether.workspace import WorkspaceError

        with patch("tether.api.sessions._workspace.clone_repo",
                   side_effect=WorkspaceError("network error")), \
             patch("tether.api.sessions._workspace.workspace_path",
                   return_value=str(tmp_path / "ws" / "sess_x")):
            response = await api_client.post(
                "/api/sessions",
                json={"clone_url": "https://github.com/owner/repo.git", "force_clone": True},
            )

        assert response.status_code == 422
        assert fresh_store.list_sessions() == []


class TestAutobranchOnClone:
    """Tests for auto_branch behaviour in POST /sessions."""

    def _make_git_repo(self, path: str) -> str:
        """Create a minimal committed git repo at *path* and return path."""
        import os
        import subprocess
        os.makedirs(path, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main", path], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "config", "user.email", "t@t.t"], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "config", "user.name", "T"], check=True, capture_output=True)
        with open(os.path.join(path, "README.md"), "w") as f:
            f.write("# test\n")
        subprocess.run(["git", "-C", path, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "commit", "-m", "init"], check=True, capture_output=True)
        return path

    def _make_workspace_result(self, path: str, branch: str | None = None):
        """Return a WorkspaceResult for the given path."""
        from tether.workspace import WorkspaceResult
        return WorkspaceResult(path=path, is_worktree=True, repo_hash="a1b2c3d4")

    @pytest.mark.anyio
    async def test_auto_branch_creates_working_branch(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path, monkeypatch
    ) -> None:
        """auto_branch=True passes working_branch to create_workspace and sets it on the session."""
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))
        clone_dir = str(tmp_path / "clone")
        import os
        os.makedirs(clone_dir)

        captured: dict = {}

        def fake_create_workspace(**kwargs):
            captured["working_branch"] = kwargs.get("working_branch")
            return self._make_workspace_result(clone_dir)

        monkeypatch.delenv("TETHER_GIT_AUTO_BRANCH", raising=False)
        with patch("tether.api.sessions._workspace.create_workspace",
                   side_effect=fake_create_workspace):
            resp = await api_client.post(
                "/api/sessions",
                json={"clone_url": "https://github.com/owner/repo.git", "auto_branch": True},
            )
        assert resp.status_code == 201
        session = resp.json()
        assert session["working_branch"] is not None
        assert session["working_branch"].startswith("tether/")
        # working_branch was forwarded to create_workspace
        assert captured["working_branch"] == session["working_branch"]

    @pytest.mark.anyio
    async def test_auto_branch_via_setting(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path, monkeypatch
    ) -> None:
        """TETHER_GIT_AUTO_BRANCH=1 triggers auto-branch even without request flag."""
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("TETHER_GIT_AUTO_BRANCH", "1")
        clone_dir = str(tmp_path / "clone")
        import os
        os.makedirs(clone_dir)

        with patch("tether.api.sessions._workspace.create_workspace",
                   return_value=self._make_workspace_result(clone_dir)):
            resp = await api_client.post(
                "/api/sessions",
                json={"clone_url": "https://github.com/owner/repo.git"},
            )
        assert resp.status_code == 201
        assert resp.json()["working_branch"] is not None

    @pytest.mark.anyio
    async def test_no_auto_branch_by_default(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path, monkeypatch
    ) -> None:
        """working_branch is None when auto_branch is False and setting is off."""
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("TETHER_GIT_AUTO_BRANCH", "0")
        clone_dir = str(tmp_path / "clone")
        import os
        os.makedirs(clone_dir)

        with patch("tether.api.sessions._workspace.create_workspace",
                   return_value=self._make_workspace_result(clone_dir)):
            resp = await api_client.post(
                "/api/sessions",
                json={"clone_url": "https://github.com/owner/repo.git", "auto_branch": False},
            )
        assert resp.status_code == 201
        assert resp.json()["working_branch"] is None

    @pytest.mark.anyio
    async def test_custom_branch_pattern(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path, monkeypatch
    ) -> None:
        """TETHER_GIT_BRANCH_PATTERN controls the branch name passed to create_workspace."""
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("TETHER_GIT_BRANCH_PATTERN", "work/{session_id}")
        monkeypatch.delenv("TETHER_GIT_AUTO_BRANCH", raising=False)
        clone_dir = str(tmp_path / "clone")
        import os
        os.makedirs(clone_dir)

        captured: dict = {}

        def fake_create_workspace(**kwargs):
            captured["working_branch"] = kwargs.get("working_branch")
            return self._make_workspace_result(clone_dir)

        with patch("tether.api.sessions._workspace.create_workspace",
                   side_effect=fake_create_workspace):
            resp = await api_client.post(
                "/api/sessions",
                json={"clone_url": "https://github.com/owner/repo.git", "auto_branch": True},
            )
        assert resp.status_code == 201
        branch = resp.json()["working_branch"]
        assert branch is not None
        assert branch.startswith("work/")
        assert captured["working_branch"] == branch

    @pytest.mark.anyio
    async def test_auto_branch_checkout_active_force_clone(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path, monkeypatch
    ) -> None:
        """With force_clone=True, the working branch is checked out in the standalone clone."""
        import subprocess
        clone_dir = self._make_git_repo(str(tmp_path / "clone"))
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.delenv("TETHER_GIT_AUTO_BRANCH", raising=False)
        with patch("tether.api.sessions._workspace.clone_repo", return_value=clone_dir), \
             patch("tether.api.sessions._workspace.workspace_path", return_value=clone_dir):
            resp = await api_client.post(
                "/api/sessions",
                json={
                    "clone_url": "https://github.com/owner/repo.git",
                    "auto_branch": True,
                    "force_clone": True,
                },
            )
        assert resp.status_code == 201
        session = resp.json()
        working_branch = session["working_branch"]
        directory = session["directory"]

        # Confirm git reports the same branch as current HEAD
        result = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == working_branch
