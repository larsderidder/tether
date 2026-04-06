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
        return self._responses.get(("GET", url), _mock_response(404))

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self._responses.get(("POST", url), _mock_response(404))

    def delete(self, url: str, **kwargs) -> httpx.Response:
        return self._responses.get(("DELETE", url), _mock_response(404))

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
    _no_bridges = _mock_response(200, {"bridges": []})

    def test_success(self, capsys):
        session = {
            "id": "new-session-id",
            "state": "AWAITING_INPUT",
            "name": "Fix the bug",
            "directory": "/home/user/project",
        }
        with _patch_client({
            ("GET", "/api/external-sessions"): _mock_response(200, []),
            ("GET", "/api/status/bridges"): self._no_bridges,
            ("POST", "/api/sessions/attach"): _mock_response(201, session),
        }):
            cli_client.cmd_attach("ext-abc", "claude_code", "/home/user/project")
        out = capsys.readouterr().out
        assert "new-session-id" in out
        assert "Fix the bug" in out

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
            ("GET", "/api/status/bridges"): self._no_bridges,
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
            ("GET", "/api/status/bridges"): self._no_bridges,
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
            ("GET", "/api/status/bridges"): self._no_bridges,
            ("POST", "/api/sessions/attach"): not_found,
        }):
            with pytest.raises(SystemExit):
                cli_client.cmd_attach("nonexistent", "claude_code", "/tmp")
        assert "not found" in capsys.readouterr().err

    def test_multiple_running_bridges_prompts_user(self, capsys, monkeypatch):
        session = {
            "id": "new-session-id",
            "state": "AWAITING_INPUT",
            "name": "Fix the bug",
            "directory": "/tmp",
        }
        two_bridges = _mock_response(
            200,
            {
                "bridges": [
                    {"platform": "discord", "status": "running"},
                    {"platform": "telegram", "status": "running"},
                ]
            },
        )
        monkeypatch.setattr("builtins.input", lambda _: "1")
        with _patch_client({
            ("GET", "/api/external-sessions"): _mock_response(200, []),
            ("GET", "/api/status/bridges"): two_bridges,
            ("POST", "/api/sessions/attach"): _mock_response(201, session),
        }):
            cli_client.cmd_attach("ext-abc", "claude_code", "/tmp")

        out = capsys.readouterr().out
        assert "new-session-id" in out


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

        def fake_list_external(directory, runner_type, limit=50):
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


class TestCmdNewClone:
    """Tests for clone-based session creation via cmd_new."""

    def _clone_session_resp(self, clone_url: str, directory: str = "/ws/sess_x/") -> httpx.Response:
        return _mock_response(201, {
            "id": "sess_clone001",
            "state": "CREATED",
            "directory": directory,
            "clone_url": clone_url,
            "adapter": None,
            "platform": None,
        })

    def test_clone_url_sent_in_request_body(self, capsys):
        """cmd_new with clone_url sends clone_url (not directory) in the body."""
        url = "https://github.com/owner/repo.git"
        captured_body: dict = {}
        resp = self._clone_session_resp(url)

        class CapturingClient(FakeClient):
            def post(self, path, **kwargs):
                captured_body.update(kwargs.get("json", {}))
                return resp

        with patch.object(cli_client, "_client", return_value=CapturingClient({})):
            cli_client.cmd_new(clone_url=url)

        assert captured_body.get("clone_url") == url
        assert "directory" not in captured_body

    def test_clone_branch_sent_in_request_body(self, capsys):
        """cmd_new with clone_branch includes it in the request body."""
        url = "https://github.com/owner/repo.git"
        captured_body: dict = {}
        resp = self._clone_session_resp(url)

        class CapturingClient(FakeClient):
            def post(self, path, **kwargs):
                captured_body.update(kwargs.get("json", {}))
                return resp

        with patch.object(cli_client, "_client", return_value=CapturingClient({})):
            cli_client.cmd_new(clone_url=url, clone_branch="feature")

        assert captured_body.get("clone_branch") == "feature"

    def test_shallow_sent_in_request_body(self, capsys):
        """cmd_new with shallow=True includes shallow=True in the request body."""
        url = "https://github.com/owner/repo.git"
        captured_body: dict = {}
        resp = self._clone_session_resp(url)

        class CapturingClient(FakeClient):
            def post(self, path, **kwargs):
                captured_body.update(kwargs.get("json", {}))
                return resp

        with patch.object(cli_client, "_client", return_value=CapturingClient({})):
            cli_client.cmd_new(clone_url=url, shallow=True)

        assert captured_body.get("shallow") is True

    def test_clone_url_shown_in_output(self, capsys):
        """Successful clone-based session shows clone_url in output."""
        url = "https://github.com/owner/repo.git"
        with _patch_client({("POST", "/api/sessions"): self._clone_session_resp(url)}):
            cli_client.cmd_new(clone_url=url)

        out = capsys.readouterr().out
        assert url in out
        assert "sess_clone001" in out

    def test_clone_prints_cloning_message(self, capsys):
        """A 'Cloning...' message is printed before the API call."""
        url = "git@github.com:owner/repo.git"
        with _patch_client({("POST", "/api/sessions"): self._clone_session_resp(url)}):
            cli_client.cmd_new(clone_url=url)

        out = capsys.readouterr().out
        assert "Cloning" in out


class TestCmdNewCloneArgParsing:
    """Tests that CLI arg parsing for --clone wires through correctly."""

    def test_clone_flag_parsed(self, monkeypatch):
        """--clone sets clone_url in cmd_new call."""
        from tether.cli import main

        called = {}

        def fake_new(**kwargs):
            called.update(kwargs)

        monkeypatch.setattr(cli_client, "cmd_new", fake_new)
        monkeypatch.setattr("tether.config.load_config", lambda: None)

        main(["new", "--clone", "https://github.com/owner/repo.git"])

        assert called.get("clone_url") == "https://github.com/owner/repo.git"
        assert called.get("directory") is None

    def test_clone_branch_flag_parsed(self, monkeypatch):
        """--branch is forwarded as clone_branch."""
        from tether.cli import main

        called = {}

        def fake_new(**kwargs):
            called.update(kwargs)

        monkeypatch.setattr(cli_client, "cmd_new", fake_new)
        monkeypatch.setattr("tether.config.load_config", lambda: None)

        main(["new", "--clone", "https://github.com/owner/repo.git", "--branch", "feat"])

        assert called.get("clone_branch") == "feat"

    def test_shallow_flag_parsed(self, monkeypatch):
        """--shallow is forwarded."""
        from tether.cli import main

        called = {}

        def fake_new(**kwargs):
            called.update(kwargs)

        monkeypatch.setattr(cli_client, "cmd_new", fake_new)
        monkeypatch.setattr("tether.config.load_config", lambda: None)

        main(["new", "--clone", "https://github.com/owner/repo.git", "--shallow"])

        assert called.get("shallow") is True

    def test_branch_without_clone_exits(self, monkeypatch, capsys):
        """--branch without --clone prints an error and exits."""
        from tether.cli import main

        monkeypatch.setattr("tether.config.load_config", lambda: None)

        with pytest.raises(SystemExit):
            main(["new", "--branch", "feature"])

        err = capsys.readouterr().err
        assert "--clone" in err

    def test_shallow_without_clone_exits(self, monkeypatch, capsys):
        """--shallow without --clone prints an error and exits."""
        from tether.cli import main

        monkeypatch.setattr("tether.config.load_config", lambda: None)

        with pytest.raises(SystemExit):
            main(["new", "--shallow"])

        err = capsys.readouterr().err
        assert "--clone" in err

    def test_clone_and_directory_mutually_exclusive(self, monkeypatch, capsys):
        """--clone with a positional directory arg prints an error and exits."""
        from tether.cli import main

        monkeypatch.setattr("tether.config.load_config", lambda: None)

        with pytest.raises(SystemExit):
            main(["new", "/some/dir", "--clone", "https://github.com/owner/repo.git"])

        err = capsys.readouterr().err
        assert "mutually exclusive" in err


class TestGitSubcommands:
    """Tests for cmd_git_* functions and their arg-parsing dispatch."""

    # -----------------------------------------------------------------------
    # cmd_git_status
    # -----------------------------------------------------------------------

    def test_git_status_prints_branch(self, capsys):
        """cmd_git_status prints branch name."""
        git_resp = _mock_response(200, {
            "branch": "main",
            "remote_url": None,
            "remote_branch": None,
            "ahead": 0,
            "behind": 0,
            "dirty": False,
            "staged_count": 0,
            "unstaged_count": 0,
            "untracked_count": 0,
            "changed_files": [],
            "last_commit": {
                "hash": "abc1234",
                "message": "Initial commit",
                "author": "Test",
                "timestamp": "2026-01-01T10:00:00+00:00",
            },
        })
        sessions_resp = _mock_response(200, [{"id": "sess_abc123", "name": "test"}])
        with _patch_client({
            ("GET", "/api/sessions"): sessions_resp,
            ("GET", "/api/sessions/sess_abc123/git"): git_resp,
        }):
            cli_client.cmd_git_status("sess_abc123")

        out = capsys.readouterr().out
        assert "main" in out
        assert "clean" in out
        assert "abc1234" in out

    def test_git_status_shows_dirty_counts(self, capsys):
        """cmd_git_status shows staged/unstaged/untracked counts when dirty."""
        git_resp = _mock_response(200, {
            "branch": "feature",
            "remote_url": None,
            "remote_branch": None,
            "ahead": 0,
            "behind": 0,
            "dirty": True,
            "staged_count": 2,
            "unstaged_count": 1,
            "untracked_count": 3,
            "changed_files": [],
            "last_commit": None,
        })
        sessions_resp = _mock_response(200, [{"id": "sess_abc123", "name": "test"}])
        with _patch_client({
            ("GET", "/api/sessions"): sessions_resp,
            ("GET", "/api/sessions/sess_abc123/git"): git_resp,
        }):
            cli_client.cmd_git_status("sess_abc123")

        out = capsys.readouterr().out
        assert "dirty" in out
        assert "2 staged" in out
        assert "1 unstaged" in out
        assert "3 untracked" in out

    # -----------------------------------------------------------------------
    # cmd_git_log
    # -----------------------------------------------------------------------

    def test_git_log_prints_commits(self, capsys):
        """cmd_git_log prints one line per commit."""
        log_resp = _mock_response(200, [
            {"hash": "abc1234", "message": "Fix bug", "author": "Alice", "timestamp": "2026-01-02T00:00:00Z"},
            {"hash": "def5678", "message": "Initial", "author": "Bob", "timestamp": "2026-01-01T00:00:00Z"},
        ])
        sessions_resp = _mock_response(200, [{"id": "sess_abc123", "name": "test"}])
        with _patch_client({
            ("GET", "/api/sessions"): sessions_resp,
            ("GET", "/api/sessions/sess_abc123/git/log"): log_resp,
        }):
            cli_client.cmd_git_log("sess_abc123", count=5)

        out = capsys.readouterr().out
        assert "abc1234" in out
        assert "Fix bug" in out
        assert "def5678" in out

    # -----------------------------------------------------------------------
    # cmd_git_commit
    # -----------------------------------------------------------------------

    def test_git_commit_prints_hash_and_message(self, capsys):
        """cmd_git_commit prints the new commit hash and message."""
        commit_resp = _mock_response(201, {
            "hash": "abc1234",
            "message": "My commit",
            "author": "Test",
            "timestamp": "2026-01-01T00:00:00Z",
        })
        sessions_resp = _mock_response(200, [{"id": "sess_abc123", "name": "test"}])
        with _patch_client({
            ("GET", "/api/sessions"): sessions_resp,
            ("POST", "/api/sessions/sess_abc123/git/commit"): commit_resp,
        }):
            cli_client.cmd_git_commit("sess_abc123", "My commit")

        out = capsys.readouterr().out
        assert "abc1234" in out
        assert "My commit" in out

    # -----------------------------------------------------------------------
    # cmd_git_push
    # -----------------------------------------------------------------------

    def test_git_push_prints_result(self, capsys):
        """cmd_git_push prints the pushed branch and remote."""
        push_resp = _mock_response(200, {
            "success": True,
            "remote": "https://github.com/owner/repo.git",
            "branch": "main",
        })
        sessions_resp = _mock_response(200, [{"id": "sess_abc123", "name": "test"}])
        with _patch_client({
            ("GET", "/api/sessions"): sessions_resp,
            ("POST", "/api/sessions/sess_abc123/git/push"): push_resp,
        }):
            cli_client.cmd_git_push("sess_abc123")

        out = capsys.readouterr().out
        assert "main" in out
        assert "github.com" in out

    # -----------------------------------------------------------------------
    # cmd_git_branch
    # -----------------------------------------------------------------------

    def test_git_branch_prints_name(self, capsys):
        """cmd_git_branch prints the created branch name."""
        branch_resp = _mock_response(200, {"branch": "feature-x"})
        sessions_resp = _mock_response(200, [{"id": "sess_abc123", "name": "test"}])
        with _patch_client({
            ("GET", "/api/sessions"): sessions_resp,
            ("POST", "/api/sessions/sess_abc123/git/branch"): branch_resp,
        }):
            cli_client.cmd_git_branch("sess_abc123", "feature-x")

        out = capsys.readouterr().out
        assert "feature-x" in out

    # -----------------------------------------------------------------------
    # cmd_git_checkout
    # -----------------------------------------------------------------------

    def test_git_checkout_prints_branch(self, capsys):
        """cmd_git_checkout prints the checked-out branch."""
        co_resp = _mock_response(200, {"branch": "develop"})
        sessions_resp = _mock_response(200, [{"id": "sess_abc123", "name": "test"}])
        with _patch_client({
            ("GET", "/api/sessions"): sessions_resp,
            ("POST", "/api/sessions/sess_abc123/git/checkout"): co_resp,
        }):
            cli_client.cmd_git_checkout("sess_abc123", "develop")

        out = capsys.readouterr().out
        assert "develop" in out

    # -----------------------------------------------------------------------
    # Arg-parsing dispatch via main()
    # -----------------------------------------------------------------------

    def test_git_status_parsed(self, monkeypatch):
        """tether git status <id> dispatches to cmd_git_status."""
        from tether.cli import main
        called = {}

        def fake(sid):
            called["session_id"] = sid

        monkeypatch.setattr(cli_client, "cmd_git_status", fake)
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        main(["git", "status", "sess_abc"])
        assert called["session_id"] == "sess_abc"

    def test_git_commit_parsed(self, monkeypatch):
        """tether git commit <id> -m 'msg' dispatches to cmd_git_commit."""
        from tether.cli import main
        called = {}

        def fake(sid, message):
            called["session_id"] = sid
            called["message"] = message

        monkeypatch.setattr(cli_client, "cmd_git_commit", fake)
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        main(["git", "commit", "sess_abc", "-m", "hello"])
        assert called["message"] == "hello"

    def test_git_push_parsed(self, monkeypatch):
        """tether git push <id> dispatches to cmd_git_push."""
        from tether.cli import main
        called = {}

        def fake(sid, remote="origin", branch=None):
            called["session_id"] = sid
            called["remote"] = remote

        monkeypatch.setattr(cli_client, "cmd_git_push", fake)
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        main(["git", "push", "sess_abc", "--remote", "upstream"])
        assert called["remote"] == "upstream"

    def test_git_branch_parsed(self, monkeypatch):
        """tether git branch <id> <name> dispatches to cmd_git_branch."""
        from tether.cli import main
        called = {}

        def fake(sid, name, checkout=True):
            called["name"] = name
            called["checkout"] = checkout

        monkeypatch.setattr(cli_client, "cmd_git_branch", fake)
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        main(["git", "branch", "sess_abc", "feature-y"])
        assert called["name"] == "feature-y"
        assert called["checkout"] is True

    def test_git_branch_no_checkout_parsed(self, monkeypatch):
        """tether git branch <id> <name> --no-checkout passes checkout=False."""
        from tether.cli import main
        called = {}

        def fake(sid, name, checkout=True):
            called["checkout"] = checkout

        monkeypatch.setattr(cli_client, "cmd_git_branch", fake)
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        main(["git", "branch", "sess_abc", "feature-z", "--no-checkout"])
        assert called["checkout"] is False

    def test_git_checkout_parsed(self, monkeypatch):
        """tether git checkout <id> <branch> dispatches to cmd_git_checkout."""
        from tether.cli import main
        called = {}

        def fake(sid, branch):
            called["branch"] = branch

        monkeypatch.setattr(cli_client, "cmd_git_checkout", fake)
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        main(["git", "checkout", "sess_abc", "main"])
        assert called["branch"] == "main"

    def test_git_log_count_parsed(self, monkeypatch):
        """tether git log <id> -n 5 passes count=5."""
        from tether.cli import main
        called = {}

        def fake(sid, count=10):
            called["count"] = count

        monkeypatch.setattr(cli_client, "cmd_git_log", fake)
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        main(["git", "log", "sess_abc", "-n", "5"])
        assert called["count"] == 5


class TestAutobranchCliFlag:
    """Tests for --auto-branch flag in tether new --clone."""

    def test_auto_branch_flag_sent_in_body(self, capsys):
        """--auto-branch passes auto_branch=True in the request body."""
        url = "https://github.com/owner/repo.git"
        captured_body: dict = {}
        resp = _mock_response(201, {
            "id": "sess_clone001",
            "state": "CREATED",
            "directory": "/ws/sess_x/",
            "clone_url": url,
            "working_branch": "tether/abc123",
            "adapter": None,
            "platform": None,
        })

        class CapturingClient(FakeClient):
            def post(self, path, **kwargs):
                captured_body.update(kwargs.get("json", {}))
                return resp

        with patch.object(cli_client, "_client", return_value=CapturingClient({})):
            cli_client.cmd_new(clone_url=url, auto_branch=True)

        assert captured_body.get("auto_branch") is True

    def test_auto_branch_shown_in_output(self, capsys):
        """Working branch is shown in cmd_new output when returned."""
        url = "https://github.com/owner/repo.git"
        resp = _mock_response(201, {
            "id": "sess_clone001",
            "state": "CREATED",
            "directory": "/ws/sess_x/",
            "clone_url": url,
            "working_branch": "tether/abc123",
            "adapter": None,
            "platform": None,
        })
        with patch.object(cli_client, "_client", return_value=FakeClient({("POST", "/api/sessions"): resp})):
            cli_client.cmd_new(clone_url=url, auto_branch=True)

        out = capsys.readouterr().out
        assert "tether/abc123" in out

    def test_auto_branch_arg_parsed(self, monkeypatch):
        """tether new --clone <url> --auto-branch passes auto_branch=True."""
        from tether.cli import main
        called = {}

        def fake_new(**kwargs):
            called.update(kwargs)

        monkeypatch.setattr(cli_client, "cmd_new", fake_new)
        monkeypatch.setattr("tether.config.load_config", lambda: None)

        main(["new", "--clone", "https://github.com/owner/repo.git", "--auto-branch"])

        assert called.get("auto_branch") is True

    def test_auto_branch_without_clone_exits(self, monkeypatch, capsys):
        """--auto-branch without --clone prints an error and exits."""
        from tether.cli import main

        monkeypatch.setattr("tether.config.load_config", lambda: None)

        with pytest.raises(SystemExit):
            main(["new", "--auto-branch"])

        err = capsys.readouterr().err
        assert "--clone" in err
