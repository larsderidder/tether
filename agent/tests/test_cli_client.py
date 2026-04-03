"""Unit tests for CLI client commands."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tether import cli_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mock_response(status_code: int = 200, json_data: dict | list | None = None) -> httpx.Response:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


class FakeClient:
    """Minimal fake httpx.Client that returns pre-configured responses."""

    def __init__(self, responses: dict[str, httpx.Response]):
        self._responses = responses

    def get(self, url: str, **kwargs) -> httpx.Response:
        response = self._responses.get(("GET", url), _mock_response(404))
        if isinstance(response, list):
            return response.pop(0) if response else _mock_response(404)
        return response

    def post(self, url: str, **kwargs) -> httpx.Response:
        response = self._responses.get(("POST", url), _mock_response(404))
        if isinstance(response, list):
            return response.pop(0) if response else _mock_response(404)
        return response

    def delete(self, url: str, **kwargs) -> httpx.Response:
        response = self._responses.get(("DELETE", url), _mock_response(404))
        if isinstance(response, list):
            return response.pop(0) if response else _mock_response(404)
        return response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _patch_client(responses: dict[tuple[str, str], httpx.Response]):
    """Patch cli_client._client to return a FakeClient."""
    fake = FakeClient(responses)
    return patch.object(cli_client, "_client", return_value=fake)


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------


class TestCmdStatus:
    def test_prints_server_info(self, capsys):
        health_resp = _mock_response(200, {"ok": True, "version": "1.2.3"})
        sessions_resp = _mock_response(200, [
            {"id": "abc123", "state": "RUNNING", "name": "test"},
            {"id": "def456", "state": "AWAITING_INPUT", "name": "other"},
        ])
        with _patch_client({
            ("GET", "/api/health"): health_resp,
            ("GET", "/api/sessions"): sessions_resp,
        }):
            cli_client.cmd_status()

        out = capsys.readouterr().out
        assert "1.2.3" in out
        assert "Sessions: 2" in out
        assert "running" in out.lower()

    def test_connection_error(self, capsys):
        with patch.object(cli_client, "_client") as mock_client:
            ctx = MagicMock()
            ctx.get.side_effect = httpx.ConnectError("refused")
            mock_client.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_client.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(SystemExit):
                cli_client.cmd_status()

        err = capsys.readouterr().err
        assert "Cannot connect" in err


# ---------------------------------------------------------------------------
# cmd_list
# ---------------------------------------------------------------------------


class TestCmdList:
    def test_no_sessions(self, capsys):
        with _patch_client({("GET", "/api/sessions"): _mock_response(200, [])}):
            cli_client.cmd_list()
        assert "No sessions" in capsys.readouterr().out

    def test_prints_table(self, capsys):
        sessions = [
            {
                "id": "abcd1234-full-uuid",
                "state": "RUNNING",
                "name": "Do something",
                "directory": "/home/user/project",
                "last_activity_at": "2025-01-01T00:00:00",
            },
        ]
        with _patch_client({("GET", "/api/sessions"): _mock_response(200, sessions)}):
            cli_client.cmd_list()
        out = capsys.readouterr().out
        assert "abcd1234" in out
        assert "running" in out
        assert "Do something" in out


# ---------------------------------------------------------------------------
# cmd_list_external
# ---------------------------------------------------------------------------


class TestCmdListExternal:
    def test_no_sessions(self, capsys):
        with _patch_client({
            ("GET", "/api/external-sessions"): _mock_response(200, []),
        }):
            cli_client.cmd_list_external(None, None)
        assert "No external sessions" in capsys.readouterr().out

    def test_prints_table(self, capsys):
        sessions = [
            {
                "id": "ext-abcd1234",
                "runner_type": "claude_code",
                "directory": "/home/user/project",
                "first_prompt": "Fix the bug",
                "last_prompt": None,
                "last_activity": "2025-01-01T00:00:00",
                "message_count": 5,
                "is_running": True,
            },
        ]
        with _patch_client({
            ("GET", "/api/external-sessions"): _mock_response(200, sessions),
        }):
            cli_client.cmd_list_external(None, None)
        out = capsys.readouterr().out
        assert "ext-abcd" in out
        assert "claude_code" in out
        assert "yes" in out


# ---------------------------------------------------------------------------
# cmd_attach
# ---------------------------------------------------------------------------


class TestCmdAttach:
    def test_success(self, capsys):
        session = {
            "id": "new-session-id",
            "state": "AWAITING_INPUT",
            "name": "Fix the bug",
            "directory": "/home/user/project",
        }
        with _patch_client({
            ("POST", "/api/sessions/attach"): _mock_response(201, session),
        }):
            cli_client.cmd_attach("ext-abc", "claude_code", "/home/user/project")
        out = capsys.readouterr().out
        assert "new-session-id" in out
        assert "Fix the bug" in out

    def test_full_id_not_found_falls_back_to_resolution(self, capsys, monkeypatch, tmp_path):
        workdir = tmp_path / "project"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        ext_sessions = [
            {
                "id": "019d5111-778a-7ce3-bb5d-05a6eb1c8539",
                "runner_type": "codex",
                "directory": str(workdir),
                "first_prompt": "marker",
            },
        ]
        not_found = _mock_response(
            404,
            {"detail": {"message": "External session not found: 019d5111-778a-7ce3-bb5d-05a6eb1c8539"}},
        )
        session = {
            "id": "new-session-id",
            "state": "AWAITING_INPUT",
            "name": "marker",
            "directory": str(workdir),
            "platform": "discord",
        }
        with _patch_client({
            ("GET", "/api/external-sessions"): _mock_response(200, ext_sessions),
            ("POST", "/api/sessions/attach"): [not_found, _mock_response(201, session)],
        }):
            cli_client.cmd_attach(
                "019d5111-778a-7ce3-bb5d-05a6eb1c8539",
                "claude_code",
                os.getcwd(),
                "discord",
            )
        out = capsys.readouterr().out
        assert "new-session-id" in out
        assert "discord" in out

    def test_prefix_resolution(self, capsys):
        """Attach resolves a short prefix against external sessions."""
        ext_sessions = [
            {
                "id": "a112db9a-full-uuid-here",
                "runner_type": "pi",
                "directory": "/home/user/project",
                "first_prompt": "Do stuff",
            },
        ]
        session = {
            "id": "new-session-id",
            "state": "AWAITING_INPUT",
            "name": "Do stuff",
            "directory": "/home/user/project",
        }
        with _patch_client({
            ("GET", "/api/external-sessions"): _mock_response(200, ext_sessions),
            ("POST", "/api/sessions/attach"): _mock_response(201, session),
        }):
            # Pass cwd as directory to trigger auto-detection from match
            cli_client.cmd_attach("a112", "claude_code", os.getcwd())
        out = capsys.readouterr().out
        assert "new-session-id" in out

    def test_ambiguous_prefix(self, capsys):
        ext_sessions = [
            {"id": "abcd1111-full", "runner_type": "pi", "directory": "/tmp", "first_prompt": "one"},
            {"id": "abcd2222-full", "runner_type": "pi", "directory": "/tmp", "first_prompt": "two"},
        ]
        with _patch_client({
            ("GET", "/api/external-sessions"): _mock_response(200, ext_sessions),
        }):
            with pytest.raises(SystemExit):
                cli_client.cmd_attach("abcd", "claude_code", "/tmp")
        err = capsys.readouterr().err
        assert "Ambiguous" in err

    def test_not_found(self, capsys):
        not_found = _mock_response(404, {"detail": {"message": "External session not found: nonexistent"}})
        not_found.raise_for_status = MagicMock()  # _check_response doesn't call this
        with _patch_client({
            ("GET", "/api/external-sessions"): _mock_response(200, []),
            ("POST", "/api/sessions/attach"): not_found,
        }):
            with pytest.raises(SystemExit):
                cli_client.cmd_attach("nonexistent", "claude_code", "/tmp")
        assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_input
# ---------------------------------------------------------------------------


class TestCmdInput:
    def test_success(self, capsys):
        sessions = [{"id": "abcd1234-full", "state": "AWAITING_INPUT", "name": "test"}]
        input_resp = _mock_response(200, {"id": "abcd1234-full", "state": "RUNNING"})
        with _patch_client({
            ("GET", "/api/sessions"): _mock_response(200, sessions),
            ("POST", "/api/sessions/abcd1234-full/input"): input_resp,
        }):
            cli_client.cmd_input("abcd", "do the thing")
        assert "Sent to" in capsys.readouterr().out

    def test_no_match(self, capsys):
        with _patch_client({("GET", "/api/sessions"): _mock_response(200, [])}):
            with pytest.raises(SystemExit):
                cli_client.cmd_input("nonexistent", "hello")
        assert "No session matching" in capsys.readouterr().err

    def test_ambiguous(self, capsys):
        sessions = [
            {"id": "abcd1111", "state": "RUNNING", "name": "one"},
            {"id": "abcd2222", "state": "RUNNING", "name": "two"},
        ]
        with _patch_client({("GET", "/api/sessions"): _mock_response(200, sessions)}):
            with pytest.raises(SystemExit):
                cli_client.cmd_input("abcd", "hello")
        err = capsys.readouterr().err
        assert "Ambiguous" in err
        assert "abcd1111" in err
        assert "abcd2222" in err


# ---------------------------------------------------------------------------
# cmd_interrupt
# ---------------------------------------------------------------------------


class TestCmdInterrupt:
    def test_success(self, capsys):
        sessions = [{"id": "abcd1234-full", "state": "RUNNING", "name": "test"}]
        interrupt_resp = _mock_response(200, {"id": "abcd1234-full", "state": "AWAITING_INPUT"})
        with _patch_client({
            ("GET", "/api/sessions"): _mock_response(200, sessions),
            ("POST", "/api/sessions/abcd1234-full/interrupt"): interrupt_resp,
        }):
            cli_client.cmd_interrupt("abcd")
        out = capsys.readouterr().out
        assert "awaiting_input" in out


# ---------------------------------------------------------------------------
# cmd_delete
# ---------------------------------------------------------------------------


class TestCmdDelete:
    def test_success(self, capsys):
        sessions = [{"id": "abcd1234-full", "state": "AWAITING_INPUT", "name": "test"}]
        with _patch_client({
            ("GET", "/api/sessions"): _mock_response(200, sessions),
            ("DELETE", "/api/sessions/abcd1234-full"): _mock_response(200, {"ok": True}),
        }):
            cli_client.cmd_delete("abcd")
        assert "Deleted" in capsys.readouterr().out

    def test_active_session(self, capsys):
        sessions = [{"id": "abcd1234-full", "state": "RUNNING", "name": "test"}]
        err_resp = _mock_response(409, {"error": {"code": "INVALID_STATE", "message": "Session is active"}})
        err_resp.raise_for_status = MagicMock()
        with _patch_client({
            ("GET", "/api/sessions"): _mock_response(200, sessions),
            ("DELETE", "/api/sessions/abcd1234-full"): err_resp,
        }):
            with pytest.raises(SystemExit):
                cli_client.cmd_delete("abcd")
        assert "active" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# cmd_open
# ---------------------------------------------------------------------------


class TestCmdOpen:
    def test_opens_browser(self, monkeypatch, capsys):
        opened = {}
        monkeypatch.setattr("webbrowser.open", lambda url: opened.update(url=url))
        cli_client.cmd_open()
        assert "127.0.0.1" in opened["url"]


# ---------------------------------------------------------------------------
# cmd_list filtering
# ---------------------------------------------------------------------------


class TestCmdListFiltering:
    def test_filter_by_state(self, capsys):
        sessions = [
            {"id": "a1", "state": "RUNNING", "name": "one", "directory": "/tmp", "last_activity_at": ""},
            {"id": "a2", "state": "AWAITING_INPUT", "name": "two", "directory": "/tmp", "last_activity_at": ""},
        ]
        with _patch_client({("GET", "/api/sessions"): _mock_response(200, sessions)}):
            cli_client.cmd_list(state="running")
        out = capsys.readouterr().out
        assert "one" in out
        assert "two" not in out

    def test_filter_by_directory(self, capsys):
        sessions = [
            {"id": "a1", "state": "RUNNING", "name": "one", "directory": "/home/user/project", "last_activity_at": ""},
            {"id": "a2", "state": "RUNNING", "name": "two", "directory": "/tmp/other", "last_activity_at": ""},
        ]
        with _patch_client({("GET", "/api/sessions"): _mock_response(200, sessions)}):
            cli_client.cmd_list(directory="/home/user/project")
        out = capsys.readouterr().out
        assert "one" in out
        assert "two" not in out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_truncate_short(self):
        assert cli_client._truncate("hello", 10) == "hello"

    def test_truncate_long(self):
        result = cli_client._truncate("a" * 40, 10)
        assert len(result) == 10
        assert result.endswith("\u2026")

    def test_truncate_none(self):
        assert cli_client._truncate(None, 10) == ""

    def test_short_id(self):
        assert cli_client._short_id("abcdef1234567890") == "abcdef123456"

    def test_base_url_defaults(self, monkeypatch):
        monkeypatch.delenv("TETHER_AGENT_HOST", raising=False)
        monkeypatch.delenv("TETHER_AGENT_PORT", raising=False)
        assert cli_client._base_url() == "http://127.0.0.1:8787"

    def test_base_url_rewrites_0000(self, monkeypatch):
        monkeypatch.setenv("TETHER_AGENT_HOST", "0.0.0.0")
        monkeypatch.setenv("TETHER_AGENT_PORT", "9000")
        assert cli_client._base_url() == "http://127.0.0.1:9000"

    def test_auth_headers_with_token(self, monkeypatch):
        monkeypatch.setenv("TETHER_AGENT_TOKEN", "secret123")
        headers = cli_client._auth_headers()
        assert headers["Authorization"] == "Bearer secret123"

    def test_auth_headers_without_token(self, monkeypatch):
        monkeypatch.delenv("TETHER_AGENT_TOKEN", raising=False)
        assert cli_client._auth_headers() == {}

    def test_build_timeout_defaults(self):
        timeout = cli_client._build_timeout()
        assert timeout.connect == 10.0
        assert timeout.read == 10.0
        assert timeout.write == 10.0
        assert timeout.pool == 10.0

    def test_mutation_timeout_defaults(self, monkeypatch):
        monkeypatch.delenv("TETHER_AGENT_MUTATION_READ_TIMEOUT_SECONDS", raising=False)
        timeout = cli_client._mutation_timeout()
        assert timeout.read == 60.0
        assert timeout.connect == 10.0

    def test_mutation_timeout_env_override(self, monkeypatch):
        monkeypatch.setenv("TETHER_AGENT_MUTATION_READ_TIMEOUT_SECONDS", "42.5")
        timeout = cli_client._mutation_timeout()
        assert timeout.read == 42.5

    def test_mutation_timeout_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("TETHER_AGENT_MUTATION_READ_TIMEOUT_SECONDS", "invalid")
        timeout = cli_client._mutation_timeout()
        assert timeout.read == 60.0


# ---------------------------------------------------------------------------
# CLI arg parsing for new subcommands
# ---------------------------------------------------------------------------


class TestCliArgParsing:
    """Test that cli.main() parses the new subcommands correctly."""

    def test_status_subcommand(self, monkeypatch):
        from tether.cli import main

        called = {}
        monkeypatch.setattr(cli_client, "cmd_status", lambda: called.update(status=True))
        monkeypatch.setattr(
            "tether.config.load_config", lambda: None
        )
        main(["status"])
        assert called.get("status")

    def test_list_subcommand(self, monkeypatch):
        from tether.cli import main

        called = {}
        monkeypatch.setattr(
            cli_client,
            "cmd_list",
            lambda state=None, directory=None: called.update(listed=True),
        )
        monkeypatch.setattr(
            "tether.config.load_config", lambda: None
        )
        main(["list"])
        assert called.get("listed")

    def test_list_external_subcommand(self, monkeypatch):
        from tether.cli import main

        called = {}

        def fake_list_external(directory, runner_type):
            called["directory"] = directory
            called["runner_type"] = runner_type

        monkeypatch.setattr(cli_client, "cmd_list_external", fake_list_external)
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        main(["list", "--external", "-d", "/tmp", "-r", "claude_code"])
        assert called["directory"] == "/tmp"
        assert called["runner_type"] == "claude_code"

    def test_interrupt_subcommand(self, monkeypatch):
        from tether.cli import main

        called = {}

        def fake_interrupt(session_id):
            called["session_id"] = session_id

        monkeypatch.setattr(cli_client, "cmd_interrupt", fake_interrupt)
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        main(["interrupt", "abc123"])
        assert called["session_id"] == "abc123"


# ---------------------------------------------------------------------------
# cmd_new
# ---------------------------------------------------------------------------


class TestCmdNew:
    def test_creates_session(self, capsys, tmp_path):
        session_resp = _mock_response(201, {
            "id": "sess_abc123def456",
            "state": "CREATED",
            "directory": str(tmp_path),
            "adapter": "claude_auto",
            "platform": None,
        })
        with _patch_client({("POST", "/api/sessions"): session_resp}):
            cli_client.cmd_new(str(tmp_path), adapter="claude_auto")

        out = capsys.readouterr().out
        assert "sess_abc123def456" in out
        assert "claude_auto" in out

    def test_creates_and_starts_with_prompt(self, capsys, tmp_path):
        session_resp = _mock_response(201, {
            "id": "sess_abc123def456",
            "state": "CREATED",
            "directory": str(tmp_path),
            "adapter": "claude_auto",
            "platform": None,
        })
        started_resp = _mock_response(200, {
            "id": "sess_abc123def456",
            "state": "RUNNING",
            "directory": str(tmp_path),
            "adapter": "claude_auto",
            "platform": None,
        })
        with _patch_client({
            ("POST", "/api/sessions"): session_resp,
            ("POST", f"/api/sessions/sess_abc123def456/start"): started_resp,
        }):
            cli_client.cmd_new(str(tmp_path), adapter="claude_auto", prompt="fix tests")

        out = capsys.readouterr().out
        assert "sess_abc123def456" in out
        assert "Started with prompt" in out

    def test_no_adapter_gives_friendly_error(self, capsys):
        error_resp = _mock_response(422, {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "No adapter specified and TETHER_DEFAULT_AGENT_ADAPTER is not configured.",
            }
        })
        with _patch_client({("POST", "/api/sessions"): error_resp}):
            with pytest.raises(SystemExit):
                cli_client.cmd_new("/tmp")

        err = capsys.readouterr().err
        assert "TETHER_DEFAULT_AGENT_ADAPTER" in err
        assert "-a" in err

    def test_new_subcommand_parsed(self, monkeypatch):
        from tether.cli import main

        called = {}

        def fake_new(directory, adapter, prompt, platform):
            called["directory"] = directory
            called["adapter"] = adapter
            called["prompt"] = prompt

        monkeypatch.setattr(cli_client, "cmd_new", fake_new)
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        main(["new", "/tmp/proj", "-a", "opencode", "-m", "fix tests"])
        assert called["adapter"] == "opencode"
        assert called["prompt"] == "fix tests"
