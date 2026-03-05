"""Tests for MCP server wrapper."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tether.store import SessionStore


class TestMCPToolDefinitions:
    """Test MCP tool definitions are properly formatted."""

    def test_mcp_tools_module_exists(self) -> None:
        """MCP tools module can be imported."""
        from tether.mcp_server import tools
        assert tools is not None

    def test_create_session_tool_defined(self) -> None:
        """create_session MCP tool is defined."""
        from tether.mcp_server.tools import get_tool_definitions

        tools = get_tool_definitions()
        tool_names = [t["name"] for t in tools]

        assert "create_session" in tool_names

    def test_send_output_tool_defined(self) -> None:
        """send_output MCP tool is defined."""
        from tether.mcp_server.tools import get_tool_definitions

        tools = get_tool_definitions()
        tool_names = [t["name"] for t in tools]

        assert "send_output" in tool_names

    def test_request_approval_tool_defined(self) -> None:
        """request_approval MCP tool is defined."""
        from tether.mcp_server.tools import get_tool_definitions

        tools = get_tool_definitions()
        tool_names = [t["name"] for t in tools]

        assert "request_approval" in tool_names

    def test_check_input_tool_defined(self) -> None:
        """check_input MCP tool is defined."""
        from tether.mcp_server.tools import get_tool_definitions

        tools = get_tool_definitions()
        tool_names = [t["name"] for t in tools]

        assert "check_input" in tool_names

    # --- Remote agent execution tool definitions ---

    def test_run_agent_tool_defined(self) -> None:
        """run_agent MCP tool is defined with required and optional fields."""
        from tether.mcp_server.tools import get_tool_definitions

        tools = get_tool_definitions()
        tool = next(t for t in tools if t["name"] == "run_agent")
        schema = tool["input_schema"]
        assert "prompt" in schema["properties"]
        assert "clone_url" in schema["properties"]
        assert "directory" in schema["properties"]
        assert "adapter" in schema["properties"]
        assert "approval_mode" in schema["properties"]
        assert "wait" in schema["properties"]
        assert schema["required"] == ["prompt"]

    def test_get_session_status_tool_defined(self) -> None:
        """get_session_status MCP tool is defined."""
        from tether.mcp_server.tools import get_tool_definitions

        tools = get_tool_definitions()
        tool_names = [t["name"] for t in tools]
        assert "get_session_status" in tool_names

    def test_get_session_output_tool_defined(self) -> None:
        """get_session_output MCP tool is defined."""
        from tether.mcp_server.tools import get_tool_definitions

        tools = get_tool_definitions()
        tool_names = [t["name"] for t in tools]
        assert "get_session_output" in tool_names

    def test_send_followup_tool_defined(self) -> None:
        """send_followup MCP tool is defined."""
        from tether.mcp_server.tools import get_tool_definitions

        tools = get_tool_definitions()
        tool_names = [t["name"] for t in tools]
        assert "send_followup" in tool_names

    def test_get_diff_tool_defined(self) -> None:
        """get_diff MCP tool is defined."""
        from tether.mcp_server.tools import get_tool_definitions

        tools = get_tool_definitions()
        tool_names = [t["name"] for t in tools]
        assert "get_diff" in tool_names

    def test_stop_session_tool_defined(self) -> None:
        """stop_session MCP tool is defined."""
        from tether.mcp_server.tools import get_tool_definitions

        tools = get_tool_definitions()
        tool_names = [t["name"] for t in tools]
        assert "stop_session" in tool_names

    def test_all_tools_have_input_schema(self) -> None:
        """Every tool definition has a valid input_schema with type=object."""
        from tether.mcp_server.tools import get_tool_definitions

        for tool in get_tool_definitions():
            assert "input_schema" in tool, f"{tool['name']} missing input_schema"
            assert tool["input_schema"]["type"] == "object", (
                f"{tool['name']} input_schema.type must be 'object'"
            )


class TestMCPToolExecution:
    """Test MCP tool execution via converged API endpoints."""

    @pytest.mark.anyio
    async def test_create_session_via_api(self, api_client, fresh_store: SessionStore) -> None:
        """MCP create_session maps to POST /api/sessions with agent fields."""
        from tether.bridges.manager import bridge_manager
        from test_external_agent_api import MockBridge

        bridge = MockBridge()
        bridge_manager.register_bridge("telegram", bridge)

        # Simulate MCP tool call via converged API
        response = await api_client.post(
            "/api/sessions",
            json={
                "agent_name": "Claude Code",
                "agent_type": "claude_code",
                "session_name": "Test MCP Session",
                "platform": "telegram",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert "id" in data
        assert data["external_agent_name"] == "Claude Code"

    @pytest.mark.anyio
    async def test_send_output_via_api(self, api_client, fresh_store: SessionStore) -> None:
        """MCP send_output maps to POST /api/sessions/{id}/events."""
        session = fresh_store.create_session("external", None)

        response = await api_client.post(
            f"/api/sessions/{session.id}/events",
            json={
                "type": "output",
                "data": {"text": "Test output from MCP"},
            },
        )

        assert response.status_code == 200
        assert response.json().get("ok") is True

    # --- Remote agent execution: run_agent ---

    @pytest.mark.anyio
    async def test_run_agent_creates_session_and_starts(
        self, api_client, fresh_store: SessionStore, tmp_path
    ) -> None:
        """run_agent tool creates a session and starts it with the prompt."""
        import subprocess

        # Set up a minimal git repo to use as the directory
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
        (repo / "README.md").write_text("# test\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

        from tether.mcp_server.tools import execute_tool

        # Patch httpx.AsyncClient to forward calls to the test api_client
        with patch("tether.mcp_server.tools._resolve_base_url", return_value="http://test"), \
             patch("tether.mcp_server.tools.httpx.AsyncClient") as MockClient:

            # Build mock responses for create and start
            create_response = MagicMock()
            create_response.raise_for_status = MagicMock()
            create_response.json.return_value = {"id": "sess_test123", "state": "created"}

            start_response = MagicMock()
            start_response.raise_for_status = MagicMock()
            start_response.json.return_value = {"id": "sess_test123", "state": "running"}

            mock_client_instance = AsyncMock()
            mock_client_instance.post = AsyncMock(side_effect=[create_response, start_response])
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client_instance

            result = await execute_tool(
                "run_agent",
                {
                    "prompt": "Fix the failing tests",
                    "directory": str(repo),
                    "approval_mode": 2,
                },
            )

        assert result["session_id"] == "sess_test123"
        assert result["session"]["state"] == "running"
        assert mock_client_instance.post.call_count == 2

        # Check first call was session creation with directory
        create_call_kwargs = mock_client_instance.post.call_args_list[0]
        assert "/api/sessions" in create_call_kwargs[0][0]
        assert create_call_kwargs[1]["json"]["directory"] == str(repo)

        # Check second call was start with the prompt
        start_call_kwargs = mock_client_instance.post.call_args_list[1]
        assert "/start" in start_call_kwargs[0][0]
        assert start_call_kwargs[1]["json"]["prompt"] == "Fix the failing tests"

    @pytest.mark.anyio
    async def test_run_agent_with_clone_url(self, fresh_store: SessionStore) -> None:
        """run_agent passes clone_url and clone_branch to session creation."""
        from tether.mcp_server.tools import execute_tool

        with patch("tether.mcp_server.tools._resolve_base_url", return_value="http://test"), \
             patch("tether.mcp_server.tools.httpx.AsyncClient") as MockClient:

            create_response = MagicMock()
            create_response.raise_for_status = MagicMock()
            create_response.json.return_value = {"id": "sess_clone1", "state": "created"}

            start_response = MagicMock()
            start_response.raise_for_status = MagicMock()
            start_response.json.return_value = {"id": "sess_clone1", "state": "running"}

            mock_client_instance = AsyncMock()
            mock_client_instance.post = AsyncMock(side_effect=[create_response, start_response])
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client_instance

            result = await execute_tool(
                "run_agent",
                {
                    "prompt": "Write unit tests",
                    "clone_url": "https://github.com/user/repo.git",
                    "branch": "feature/tests",
                    "adapter": "claude_auto",
                    "approval_mode": 2,
                },
            )

        assert result["session_id"] == "sess_clone1"
        create_kwargs = mock_client_instance.post.call_args_list[0][1]["json"]
        assert create_kwargs["clone_url"] == "https://github.com/user/repo.git"
        assert create_kwargs["clone_branch"] == "feature/tests"
        assert create_kwargs["adapter"] == "claude_auto"

    # --- get_session_status ---

    @pytest.mark.anyio
    async def test_get_session_status_via_api(
        self, api_client, fresh_store: SessionStore
    ) -> None:
        """get_session_status returns state and metadata for a session."""
        session = fresh_store.create_session("myrepo", None)

        response = await api_client.get(f"/api/sessions/{session.id}")
        assert response.status_code == 200

        # Now test via the MCP tool with a mocked client
        from tether.mcp_server.tools import execute_tool

        with patch("tether.mcp_server.tools._resolve_base_url", return_value="http://test"), \
             patch("tether.mcp_server.tools.httpx.AsyncClient") as MockClient:

            status_response = MagicMock()
            status_response.raise_for_status = MagicMock()
            status_response.json.return_value = {
                "id": session.id,
                "state": "created",
                "name": None,
                "summary": None,
                "started_at": None,
                "ended_at": None,
                "last_activity_at": "2026-01-01T00:00:00",
                "exit_code": None,
                "directory": None,
                "working_branch": None,
                "clone_url": None,
                "has_pending_permission": False,
                "adapter": None,
            }

            mock_client_instance = AsyncMock()
            mock_client_instance.get = AsyncMock(return_value=status_response)
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client_instance

            result = await execute_tool("get_session_status", {"session_id": session.id})

        assert result["session_id"] == session.id
        assert result["state"] == "created"
        assert "last_activity_at" in result
        assert "working_branch" in result

    # --- get_session_output ---

    @pytest.mark.anyio
    async def test_get_session_output_polls_output_events(
        self, api_client, fresh_store: SessionStore
    ) -> None:
        """get_session_output polls the event log for output/state/error events."""
        session = fresh_store.create_session("myrepo", None)

        from tether.mcp_server.tools import execute_tool

        with patch("tether.mcp_server.tools._resolve_base_url", return_value="http://test"), \
             patch("tether.mcp_server.tools.httpx.AsyncClient") as MockClient:

            output_response = MagicMock()
            output_response.raise_for_status = MagicMock()
            output_response.json.return_value = {
                "events": [
                    {"type": "output", "data": {"text": "Done!"}, "seq": 1},
                ]
            }

            mock_client_instance = AsyncMock()
            mock_client_instance.get = AsyncMock(return_value=output_response)
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client_instance

            result = await execute_tool(
                "get_session_output", {"session_id": session.id, "since_seq": 0}
            )

        assert "events" in result
        assert result["events"][0]["type"] == "output"

        # Verify correct types filter was passed
        call_params = mock_client_instance.get.call_args[1]["params"]
        assert "output" in call_params["types"]
        assert call_params["since_seq"] == 0

    # --- send_followup ---

    @pytest.mark.anyio
    async def test_send_followup_via_api(
        self, api_client, fresh_store: SessionStore, monkeypatch
    ) -> None:
        """send_followup maps to POST /api/sessions/{id}/input."""
        session = fresh_store.create_session("myrepo", None)
        # Transition CREATED -> RUNNING -> AWAITING_INPUT to satisfy state machine
        from tether.models import SessionState
        from tether.api.state import transition
        transition(session, SessionState.RUNNING, started_at=True)
        transition(session, SessionState.AWAITING_INPUT)
        fresh_store.update_session(session)

        # Patch the runner so no real backend is needed
        class FakeRunner:
            runner_type = "fake"

            async def send_input(self, session_id: str, text: str) -> None:
                pass

        monkeypatch.setattr(
            "tether.api.sessions.get_api_runner", lambda *args, **kwargs: FakeRunner()
        )

        response = await api_client.post(
            f"/api/sessions/{session.id}/input",
            json={"text": "Please also update the docs"},
        )
        assert response.status_code == 200

    # --- get_diff ---

    @pytest.mark.anyio
    async def test_get_diff_via_api(
        self, api_client, fresh_store: SessionStore
    ) -> None:
        """get_diff maps to GET /api/sessions/{id}/diff."""
        session = fresh_store.create_session("myrepo", None)

        response = await api_client.get(f"/api/sessions/{session.id}/diff")
        assert response.status_code == 200
        data = response.json()
        assert "diff" in data
        assert "files" in data

    # --- stop_session ---

    @pytest.mark.anyio
    async def test_stop_session_on_non_running_session(
        self, api_client, fresh_store: SessionStore
    ) -> None:
        """stop_session returns 409 when session is not running."""
        session = fresh_store.create_session("myrepo", None)

        response = await api_client.post(f"/api/sessions/{session.id}/interrupt")
        # CREATED state cannot be interrupted
        assert response.status_code == 409

    # --- _resolve_base_url ---

    def test_resolve_base_url_uses_env_var(self, monkeypatch) -> None:
        """_resolve_base_url uses TETHER_API_URL when set."""
        monkeypatch.setenv("TETHER_API_URL", "https://remote.example.com:9000")
        from importlib import reload
        import tether.mcp_server.tools as tools_module
        # Call directly since the env var is read at call time
        result = tools_module._resolve_base_url()
        assert result == "https://remote.example.com:9000"

    def test_resolve_base_url_falls_back_to_localhost(self, monkeypatch) -> None:
        """_resolve_base_url falls back to localhost:{port} when TETHER_API_URL is unset."""
        monkeypatch.delenv("TETHER_API_URL", raising=False)
        from tether.mcp_server.tools import _resolve_base_url
        from tether.settings import settings
        result = _resolve_base_url()
        assert result == f"http://localhost:{settings.port()}"


class TestMCPServerIntegration:
    """Test MCP server can be started and responds to requests."""

    def test_mcp_server_module_exists(self) -> None:
        """MCP server module can be imported."""
        from tether.mcp_server import server
        assert server is not None

    def test_mcp_server_has_main_function(self) -> None:
        """MCP server has a main() entry point."""
        from tether.mcp_server.server import main

        assert callable(main)

    def test_tool_count_matches_definitions(self) -> None:
        """Server logs the correct number of tools from get_tool_definitions."""
        from tether.mcp_server.tools import get_tool_definitions

        expected_tools = {
            "create_session",
            "send_output",
            "request_approval",
            "check_input",
            "run_agent",
            "get_session_status",
            "get_session_output",
            "send_followup",
            "get_diff",
            "stop_session",
        }
        defined = {t["name"] for t in get_tool_definitions()}
        assert expected_tools == defined
