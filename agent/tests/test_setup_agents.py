"""Tests for the /api/setup/agents endpoints and the cmd_setup_agents CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


class TestSetupAgentsListEndpoint:
    """Tests for GET /api/setup/agents."""

    @pytest.mark.anyio
    async def test_returns_known_agents(self, api_client: httpx.AsyncClient) -> None:
        """Endpoint returns all known agents with required fields."""
        response = await api_client.get("/api/setup/agents")

        assert response.status_code == 200
        data = response.json()
        assert "agents" in data
        names = {a["name"] for a in data["agents"]}
        assert "claude_code" in names
        assert "opencode" in names
        assert "pi" in names

    @pytest.mark.anyio
    async def test_agent_fields_present(self, api_client: httpx.AsyncClient) -> None:
        """Each agent entry has all required fields."""
        response = await api_client.get("/api/setup/agents")
        agents = response.json()["agents"]

        required = {
            "name",
            "binary",
            "installed",
            "version",
            "authenticated",
            "install_command",
        }
        for agent in agents:
            assert required.issubset(agent.keys()), f"Missing fields in {agent}"

    @pytest.mark.anyio
    async def test_installed_reflects_which(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """'installed' field reflects whether the binary is on PATH."""
        # claude binary is almost certainly not installed in test env.
        with patch("shutil.which", return_value=None):
            response = await api_client.get("/api/setup/agents")

        agents = {a["name"]: a for a in response.json()["agents"]}
        assert agents["claude_code"]["installed"] is False
        assert agents["claude_code"]["version"] is None

    @pytest.mark.anyio
    async def test_installed_true_when_binary_found(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """'installed' is True when the binary is found on PATH."""

        def fake_which(cmd: str) -> str | None:
            if cmd == "claude":
                return "/usr/local/bin/claude"
            return None

        with patch("shutil.which", side_effect=fake_which):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="1.2.3\n", stderr=""
                )
                response = await api_client.get("/api/setup/agents")

        agents = {a["name"]: a for a in response.json()["agents"]}
        assert agents["claude_code"]["installed"] is True


class TestSetupAgentsInstallEndpoint:
    """Tests for POST /api/setup/agents/{name}/install."""

    @pytest.mark.anyio
    async def test_install_unknown_agent_returns_404(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Installing an unknown agent name returns 404."""
        response = await api_client.post("/api/setup/agents/unknown_agent/install")
        assert response.status_code == 404

    @pytest.mark.anyio
    async def test_install_pi_returns_400_no_command(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """'pi' has no install_command; should return 400."""
        response = await api_client.post("/api/setup/agents/pi/install")
        assert response.status_code == 400

    @pytest.mark.anyio
    async def test_install_claude_code_success(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Successful install returns ok=True and version."""
        with patch("subprocess.run") as mock_run:
            # First call is the install command, second is --version check.
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="1.0.5\n", stderr=""),
            ]
            with patch("shutil.which", return_value="/usr/local/bin/claude"):
                response = await api_client.post(
                    "/api/setup/agents/claude_code/install"
                )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["agent"] == "claude_code"

    @pytest.mark.anyio
    async def test_install_failure_returns_500(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Non-zero exit from install command returns 500."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="npm not found"
            )
            response = await api_client.post("/api/setup/agents/claude_code/install")

        assert response.status_code == 500
        data = response.json()
        assert "error" in data


class TestSetupAgentsCredentialsEndpoint:
    """Tests for POST /api/setup/agents/{name}/credentials."""

    @pytest.mark.anyio
    async def test_unknown_agent_returns_404(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Unknown agent name returns 404."""
        response = await api_client.post(
            "/api/setup/agents/unknown/credentials",
            json={"files": {}},
        )
        assert response.status_code == 404

    @pytest.mark.anyio
    async def test_writes_credentials_file(
        self,
        api_client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Credentials are written to the correct path under home."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        creds_content = json.dumps({"refreshToken": "tok123"})
        response = await api_client.post(
            "/api/setup/agents/claude_code/credentials",
            json={"files": {".claude/.credentials.json": creds_content}},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert ".claude/.credentials.json" in data["files_written"]

        written = (tmp_path / ".claude" / ".credentials.json").read_text()
        assert written == creds_content

    @pytest.mark.anyio
    async def test_rejects_absolute_paths(
        self,
        api_client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Absolute paths in the files dict are rejected."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        response = await api_client.post(
            "/api/setup/agents/claude_code/credentials",
            json={"files": {"/etc/passwd": "bad"}},
        )

        assert response.status_code == 400

    @pytest.mark.anyio
    async def test_rejects_path_traversal(
        self,
        api_client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Path traversal attempts (../) are rejected."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        response = await api_client.post(
            "/api/setup/agents/claude_code/credentials",
            json={"files": {"../../etc/passwd": "bad"}},
        )

        assert response.status_code == 400

    @pytest.mark.anyio
    async def test_created_file_has_restricted_permissions(
        self,
        api_client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Written credential file is chmod 600."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        response = await api_client.post(
            "/api/setup/agents/claude_code/credentials",
            json={"files": {".claude/.credentials.json": "{}"}},
        )

        assert response.status_code == 200
        target = tmp_path / ".claude" / ".credentials.json"
        mode = oct(target.stat().st_mode)[-3:]
        assert mode == "600"


class TestSetupAgentsVerifyEndpoint:
    """Tests for POST /api/setup/agents/{name}/verify."""

    @pytest.mark.anyio
    async def test_unknown_agent_returns_404(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Unknown agent name returns 404."""
        response = await api_client.post("/api/setup/agents/unknown/verify")
        assert response.status_code == 404

    @pytest.mark.anyio
    async def test_not_installed_returns_ok_false(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """Verify returns ok=False when the binary is missing."""
        with patch("shutil.which", return_value=None):
            response = await api_client.post("/api/setup/agents/claude_code/verify")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "not installed" in data["message"]

    @pytest.mark.anyio
    async def test_installed_no_creds_returns_ok_false(
        self,
        api_client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify returns ok=False when installed but credentials are missing."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="1.0.0\n", stderr=""
                )
                response = await api_client.post("/api/setup/agents/claude_code/verify")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert data["authenticated"] is False

    @pytest.mark.anyio
    async def test_installed_with_creds_returns_ok_true(
        self,
        api_client: httpx.AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify returns ok=True when installed and credentials exist."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        # Create a credentials file so _is_authenticated returns True.
        creds_dir = tmp_path / ".claude"
        creds_dir.mkdir()
        (creds_dir / ".credentials.json").write_text('{"token":"x"}')

        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="1.0.0\n", stderr=""
                )
                response = await api_client.post("/api/setup/agents/claude_code/verify")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["authenticated"] is True

    @pytest.mark.anyio
    async def test_agent_without_creds_concept_returns_ok_when_installed(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """An agent with no credentials_path (e.g. pi) is ok when just installed."""
        with patch(
            "shutil.which", side_effect=lambda b: "/usr/bin/pi" if b == "pi" else None
        ):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="0.5.0\n", stderr=""
                )
                response = await api_client.post("/api/setup/agents/pi/verify")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestCmdSetupAgents:
    """Tests for the cmd_setup_agents CLI function."""

    def test_check_only_prints_status_no_prompts(
        self, capsys, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--check prints agent status and does not prompt."""
        from tether.cli_client import cmd_setup_agents

        agents_data = {
            "agents": [
                {
                    "name": "claude_code",
                    "binary": "claude",
                    "installed": False,
                    "version": None,
                    "authenticated": False,
                    "install_command": "npm install -g @anthropic-ai/claude-code",
                },
            ]
        }

        with patch("tether.cli_client._get_json", return_value=agents_data):
            cmd_setup_agents(check_only=True)

        out = capsys.readouterr().out
        assert "claude_code" in out.lower() or "claude" in out.lower()
        # No prompts means no Y/n in the output.
        assert "[Y/n]" not in out

    def test_unknown_agent_filter_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Filtering to an unknown agent name exits with status 1."""
        from tether.cli_client import cmd_setup_agents

        agents_data = {"agents": []}
        with patch("tether.cli_client._get_json", return_value=agents_data):
            with pytest.raises(SystemExit) as exc_info:
                cmd_setup_agents(agent_filter="nonexistent_agent")
            assert exc_info.value.code == 1

    def test_already_installed_and_authenticated_no_prompts(
        self, capsys, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fully set-up agents produce no install/credential prompts."""
        from tether.cli_client import cmd_setup_agents

        agents_data = {
            "agents": [
                {
                    "name": "claude_code",
                    "binary": "claude",
                    "installed": True,
                    "version": "1.0.0",
                    "authenticated": True,
                    "install_command": "npm install -g @anthropic-ai/claude-code",
                },
            ]
        }

        with patch("tether.cli_client._get_json", return_value=agents_data):
            cmd_setup_agents()

        out = capsys.readouterr().out
        assert "[Y/n]" not in out

    def test_install_flow_on_confirmation(
        self, capsys, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When user confirms, install and verify are called."""
        from tether.cli_client import cmd_setup_agents

        agents_data = {
            "agents": [
                {
                    "name": "claude_code",
                    "binary": "claude",
                    "installed": False,
                    "version": None,
                    "authenticated": False,
                    "install_command": "npm install -g @anthropic-ai/claude-code",
                },
            ]
        }

        install_result = {
            "ok": True,
            "agent": "claude_code",
            "version": "1.2.0",
            "message": "Installed",
        }
        verify_result = {
            "ok": True,
            "agent": "claude_code",
            "version": "1.2.0",
            "authenticated": True,
            "message": "ready",
        }

        prompts = iter([True, False])  # install=yes, push creds=no

        with patch("tether.cli_client._get_json", return_value=agents_data):
            with patch("tether.cli_client._post_json") as mock_post:
                mock_post.side_effect = [install_result, verify_result]
                with patch("tether.cli_client._prompt", side_effect=prompts):
                    with patch(
                        "tether.cli_client._read_local_credentials", return_value={}
                    ):
                        cmd_setup_agents(all_agents=True)

        out = capsys.readouterr().out
        assert "Installed" in out or "1.2.0" in out

    def test_push_credentials_flow(
        self, capsys, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When agent is installed but not authenticated, credentials are pushed."""
        from tether.cli_client import cmd_setup_agents

        agents_data = {
            "agents": [
                {
                    "name": "claude_code",
                    "binary": "claude",
                    "installed": True,
                    "version": "1.0.0",
                    "authenticated": False,
                    "install_command": "npm install -g @anthropic-ai/claude-code",
                },
            ]
        }

        creds = {".claude/.credentials.json": '{"refreshToken": "tok"}'}
        verify_result = {
            "ok": True,
            "agent": "claude_code",
            "version": "1.0.0",
            "authenticated": True,
            "message": "ready",
        }
        creds_result = {
            "ok": True,
            "agent": "claude_code",
            "files_written": [".claude/.credentials.json"],
        }

        with patch("tether.cli_client._get_json", return_value=agents_data):
            with patch("tether.cli_client._post_json") as mock_post:
                mock_post.side_effect = [creds_result, verify_result]
                with patch("tether.cli_client._prompt", return_value=True):
                    with patch(
                        "tether.cli_client._read_local_credentials", return_value=creds
                    ):
                        cmd_setup_agents()

        out = capsys.readouterr().out
        assert "Credentials installed" in out or "authenticated" in out.lower()

    def test_read_local_credentials_returns_empty_when_no_file(self) -> None:
        """_read_local_credentials returns empty dict when credentials file is absent."""
        from tether.cli_client import _read_local_credentials

        with patch("os.path.exists", return_value=False):
            result = _read_local_credentials("claude_code")

        assert result == {}

    def test_read_local_credentials_returns_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_read_local_credentials returns file content when credentials file exists."""
        from tether.cli_client import _read_local_credentials

        creds_dir = tmp_path / ".claude"
        creds_dir.mkdir()
        creds_file = creds_dir / ".credentials.json"
        creds_file.write_text('{"refreshToken": "secret"}')

        # Point expanduser("~") to tmp_path so the function finds the file.
        monkeypatch.setenv("HOME", str(tmp_path))

        result = _read_local_credentials("claude_code")

        assert ".claude/.credentials.json" in result
        assert "refreshToken" in result[".claude/.credentials.json"]

    def test_read_local_credentials_unknown_agent_returns_empty(self) -> None:
        """_read_local_credentials returns {} for agents with no creds concept."""
        from tether.cli_client import _read_local_credentials

        result = _read_local_credentials("opencode")
        assert result == {}

        result = _read_local_credentials("pi")
        assert result == {}
