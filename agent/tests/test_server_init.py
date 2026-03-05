"""Tests for the tether server init SSH bootstrap feature."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from tether.server_init import (
    _meets_python_requirement,
    _register_locally,
    _write_remote_config,
    run_server_init,
    ssh_check,
    ssh_run,
)
from tether.servers import get_server, load_servers, write_server


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def servers_file(tmp_path) -> Path:
    """Return a temporary path for servers.yaml."""
    return tmp_path / "servers.yaml"


def _make_ssh_mock(responses: dict[str, tuple[int, str, str]]):
    """Return a mock for ``ssh_run`` that dispatches by command substring.

    *responses* maps a substring of the command to the return value.
    The first matching key wins.  A key of ``"*"`` acts as a wildcard
    fallback.
    """

    def _mock(host, command, *, user=None, timeout=60, quiet=False):
        for key, value in responses.items():
            if key == "*" or key in command:
                return value
        return (0, "", "")

    return _mock


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------


class TestMeetsPythonRequirement:
    def test_311_passes(self):
        assert _meets_python_requirement("3.11") is True

    def test_312_passes(self):
        assert _meets_python_requirement("3.12") is True

    def test_310_fails(self):
        assert _meets_python_requirement("3.10") is False

    def test_none_fails(self):
        assert _meets_python_requirement(None) is False

    def test_garbage_fails(self):
        assert _meets_python_requirement("not-a-version") is False

    def test_400_passes(self):
        assert _meets_python_requirement("4.0") is True


class TestSshRun:
    def test_returns_stdout(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "hello\n"
        mock_result.stderr = ""

        with patch(
            "tether.server_init.subprocess.run", return_value=mock_result
        ) as mock_run:
            rc, out, err = ssh_run("myhost", "echo hello")

        assert rc == 0
        assert out == "hello"
        assert err == ""
        # Should include BatchMode and the host
        cmd = mock_run.call_args[0][0]
        assert "myhost" in cmd
        assert "echo hello" in cmd

    def test_includes_user_in_target(self, monkeypatch):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch(
            "tether.server_init.subprocess.run", return_value=mock_result
        ) as mock_run:
            ssh_run("myhost", "true", user="alice")

        cmd = mock_run.call_args[0][0]
        assert "alice@myhost" in cmd


class TestSshCheck:
    def test_returns_true_on_rc0(self):
        with patch("tether.server_init.ssh_run", return_value=(0, "", "")) as mock:
            result = ssh_check("myhost")
        assert result is True

    def test_returns_false_on_nonzero(self):
        with patch(
            "tether.server_init.ssh_run", return_value=(1, "", "connect failed")
        ):
            result = ssh_check("myhost")
        assert result is False


# ---------------------------------------------------------------------------
# Unit tests: servers.py
# ---------------------------------------------------------------------------


class TestWriteServer:
    def test_creates_file(self, servers_file):
        write_server(
            "work",
            {"host": "10.0.0.1", "port": "8787", "token": "abc"},
            path=servers_file,
        )
        assert servers_file.exists()

    def test_reads_back(self, servers_file):
        write_server(
            "work",
            {"host": "10.0.0.1", "port": "8787", "token": "abc"},
            path=servers_file,
        )
        entry = get_server("work", path=servers_file)
        assert entry is not None
        assert entry["host"] == "10.0.0.1"
        assert entry["port"] == "8787"
        assert entry["token"] == "abc"

    def test_preserves_other_entries(self, servers_file):
        write_server(
            "alpha",
            {"host": "1.2.3.4", "port": "8787", "token": "aaa"},
            path=servers_file,
        )
        write_server(
            "beta",
            {"host": "5.6.7.8", "port": "9000", "token": "bbb"},
            path=servers_file,
        )
        alpha = get_server("alpha", path=servers_file)
        assert alpha is not None
        assert alpha["host"] == "1.2.3.4"
        beta = get_server("beta", path=servers_file)
        assert beta is not None
        assert beta["host"] == "5.6.7.8"

    def test_updates_existing_entry(self, servers_file):
        write_server(
            "work",
            {"host": "1.2.3.4", "port": "8787", "token": "old"},
            path=servers_file,
        )
        write_server(
            "work",
            {"host": "1.2.3.4", "port": "8787", "token": "new"},
            path=servers_file,
        )
        entry = get_server("work", path=servers_file)
        assert entry is not None
        assert entry["token"] == "new"

    def test_set_default(self, servers_file):
        write_server(
            "work",
            {"host": "1.2.3.4", "port": "8787", "token": "t"},
            path=servers_file,
            set_default=True,
        )
        data = load_servers(path=servers_file)
        assert data.get("default") == "work"

    def test_does_not_set_default_by_default(self, servers_file):
        write_server(
            "work", {"host": "1.2.3.4", "port": "8787", "token": "t"}, path=servers_file
        )
        data = load_servers(path=servers_file)
        assert "default" not in data


# ---------------------------------------------------------------------------
# Unit tests: _register_locally
# ---------------------------------------------------------------------------


class TestRegisterLocally:
    def test_writes_server_entry(self, tmp_path):
        servers_file = str(tmp_path / "servers.yaml")
        logs: list[str] = []
        result = MagicMock()
        result.steps_completed = []

        _register_locally(
            alias="myserver",
            host="10.0.0.1",
            port=8787,
            token="tok123",
            servers_path_override=servers_file,
            log=logs.append,
            result=result,
        )

        entry = get_server("myserver", path=Path(servers_file))
        assert entry is not None
        assert entry["host"] == "10.0.0.1"
        assert entry["port"] == "8787"
        assert entry["token"] == "tok123"
        assert "register_locally" in result.steps_completed


# ---------------------------------------------------------------------------
# Integration tests: run_server_init with mocked ssh_run
# ---------------------------------------------------------------------------


class TestRunServerInit:
    """End-to-end tests that mock ``ssh_run`` and verify the call sequence."""

    def _make_responses(
        self, pkg_manager: str = "apt"
    ) -> dict[str, tuple[int, str, str]]:
        """Build a plausible set of SSH responses for a clean Debian host."""
        return {
            "true": (0, "", ""),  # ssh_check
            # OS detection
            "apt-get": (0, pkg_manager, ""),
            # Python check
            "sys.version_info": (0, "3.12", ""),
            # pipx check
            "command -v pipx": (0, "/usr/bin/pipx", ""),
            # node check
            "command -v node": (0, "/usr/bin/node", ""),
            # git check
            "command -v git": (0, "/usr/bin/git", ""),
            # tether already installed check
            "pipx list": (0, "tether-ai", ""),
            # upgrade tether
            "pipx upgrade tether-ai": (0, "upgraded", ""),
            # detect tether binary
            "command -v tether": (0, "/home/user/.local/bin/tether", ""),
            # write config
            "mkdir -p ~/.config/tether": (0, "", ""),
            # systemd available
            "command -v systemctl": (0, "/bin/systemctl", ""),
            # sudo available
            "sudo -n true": (0, "", ""),
            # get home dir
            "echo $HOME": (0, "/home/user", ""),
            # systemd install
            "tee /etc/systemd/system/tether.service": (0, "", ""),
            "daemon-reload": (0, "", ""),
            "systemctl enable tether": (0, "", ""),
            "systemctl restart tether": (0, "", ""),
            # health check
            "curl -sf": (0, "ok", ""),
            # wildcard fallback
            "*": (0, "", ""),
        }

    def test_successful_run_registers_server(self, tmp_path, monkeypatch):
        servers_file = str(tmp_path / "servers.yaml")
        responses = self._make_responses()
        monkeypatch.setattr("tether.server_init.ssh_run", _make_ssh_mock(responses))

        result = run_server_init(
            "10.0.0.1",
            name="testserver",
            port=8787,
            servers_path_override=servers_file,
            health_timeout=1,
        )

        assert result.name == "testserver"
        assert result.host == "10.0.0.1"
        assert result.port == 8787
        assert len(result.token) == 64  # secrets.token_hex(32)
        assert "register_locally" in result.steps_completed

        entry = get_server("testserver", path=Path(servers_file))
        assert entry is not None
        assert entry["host"] == "10.0.0.1"
        assert entry["token"] == result.token

    def test_ssh_failure_raises_connection_error(self, monkeypatch):
        monkeypatch.setattr(
            "tether.server_init.ssh_run", lambda *a, **kw: (1, "", "Connection refused")
        )
        with pytest.raises(ConnectionError):
            run_server_init("badhost", health_timeout=1)

    def test_idempotent_rerun_updates_server_entry(self, tmp_path, monkeypatch):
        """Running server init a second time should update the servers.yaml entry."""
        servers_file = str(tmp_path / "servers.yaml")
        responses = self._make_responses()

        # First run
        monkeypatch.setattr("tether.server_init.ssh_run", _make_ssh_mock(responses))
        result1 = run_server_init(
            "10.0.0.1",
            name="myserver",
            port=8787,
            servers_path_override=servers_file,
            health_timeout=1,
        )

        # Second run (simulates idempotent re-run)
        result2 = run_server_init(
            "10.0.0.1",
            name="myserver",
            port=8787,
            servers_path_override=servers_file,
            health_timeout=1,
        )

        # Entry is updated with the new token
        entry = get_server("myserver", path=Path(servers_file))
        assert entry is not None
        assert entry["token"] == result2.token
        # Tokens differ between runs (different secrets)
        assert result1.token != result2.token

    def test_python_already_installed_skipped(self, tmp_path, monkeypatch):
        servers_file = str(tmp_path / "servers.yaml")
        responses = self._make_responses()
        monkeypatch.setattr("tether.server_init.ssh_run", _make_ssh_mock(responses))

        result = run_server_init(
            "10.0.0.1",
            name="s",
            port=8787,
            servers_path_override=servers_file,
            health_timeout=1,
        )

        assert "install_python" in result.steps_skipped

    def test_log_callback_receives_messages(self, tmp_path, monkeypatch):
        servers_file = str(tmp_path / "servers.yaml")
        responses = self._make_responses()
        monkeypatch.setattr("tether.server_init.ssh_run", _make_ssh_mock(responses))

        messages: list[str] = []
        run_server_init(
            "10.0.0.1",
            name="s",
            port=8787,
            servers_path_override=servers_file,
            health_timeout=1,
            log=messages.append,
        )

        assert any("SSH connection OK" in m for m in messages)
        assert any("tether-ai" in m for m in messages)

    def test_health_timeout_skips_step(self, tmp_path, monkeypatch):
        servers_file = str(tmp_path / "servers.yaml")
        responses = self._make_responses()
        # Health check always fails
        responses["curl -sf"] = (1, "", "")
        monkeypatch.setattr("tether.server_init.ssh_run", _make_ssh_mock(responses))
        # Also mock time.sleep to keep test fast
        monkeypatch.setattr("tether.server_init.time.sleep", lambda s: None)

        result = run_server_init(
            "10.0.0.1",
            name="s",
            port=8787,
            servers_path_override=servers_file,
            health_timeout=1,
        )

        assert "health_check" in result.steps_skipped
        # Server is still registered even if health check timed out
        assert "register_locally" in result.steps_completed

    def test_no_systemd_skips_service_setup(self, tmp_path, monkeypatch):
        servers_file = str(tmp_path / "servers.yaml")
        responses = self._make_responses()
        # systemd not available
        responses["command -v systemctl"] = (1, "", "")
        monkeypatch.setattr("tether.server_init.ssh_run", _make_ssh_mock(responses))

        result = run_server_init(
            "10.0.0.1",
            name="s",
            port=8787,
            servers_path_override=servers_file,
            health_timeout=1,
        )

        assert "systemd" in result.steps_skipped

    def test_telegram_config_included(self, tmp_path, monkeypatch):
        servers_file = str(tmp_path / "servers.yaml")
        responses = self._make_responses()

        written_cmds: list[str] = []

        def capturing_ssh_run(host, command, *, user=None, timeout=60, quiet=False):
            written_cmds.append(command)
            return _make_ssh_mock(responses)(
                host, command, user=user, timeout=timeout, quiet=quiet
            )

        monkeypatch.setattr("tether.server_init.ssh_run", capturing_ssh_run)

        run_server_init(
            "10.0.0.1",
            name="s",
            port=8787,
            telegram_token="bot:TOKEN",
            telegram_group_id="-100123456",
            servers_path_override=servers_file,
            health_timeout=1,
        )

        config_cmd = next((c for c in written_cmds if "TETHER_AGENT_TOKEN" in c), None)
        assert config_cmd is not None
        assert "TELEGRAM_BOT_TOKEN" in config_cmd
        assert "TELEGRAM_FORUM_GROUP_ID" in config_cmd


# ---------------------------------------------------------------------------
# CLI argument parsing tests
# ---------------------------------------------------------------------------


class TestServerCliParsing:
    def test_server_init_parses_host(self):
        import argparse

        # Re-parse using the actual main parser setup
        from tether.cli import main
        import sys

        # Capture the args namespace without executing
        captured: list = []

        def fake_run_server_init(args):
            captured.append(args)

        with patch("tether.cli._run_server_init", fake_run_server_init):
            main(["server", "init", "myhost.local"])

        assert len(captured) == 1
        assert captured[0].host == "myhost.local"

    def test_server_init_parses_options(self):
        from tether.cli import main

        captured: list = []

        def fake_run_server_init(args):
            captured.append(args)

        with patch("tether.cli._run_server_init", fake_run_server_init):
            main(
                [
                    "server",
                    "init",
                    "myhost",
                    "--name",
                    "prod",
                    "--user",
                    "deploy",
                    "--port",
                    "9000",
                ]
            )

        assert captured[0].name == "prod"
        assert captured[0].user == "deploy"
        assert captured[0].port == 9000

    def test_server_status_parses_name(self):
        from tether.cli import main

        captured: list = []

        def fake_run(args):
            captured.append(args)

        with patch("tether.cli._run_server_status", fake_run):
            main(["server", "status", "production"])

        assert captured[0].name == "production"

    def test_server_logs_parses_options(self):
        from tether.cli import main

        captured: list = []

        def fake_run(args):
            captured.append(args)

        with patch("tether.cli._run_server_logs", fake_run):
            main(["server", "logs", "prod", "--lines", "100", "--follow"])

        assert captured[0].name == "prod"
        assert captured[0].lines == 100
        assert captured[0].follow is True
