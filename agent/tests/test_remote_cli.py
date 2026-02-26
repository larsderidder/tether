"""Tests for remote CLI connection flags (--host, --port, --token, --server)
and the servers.yaml config loader."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tether.servers import get_default_server, get_server, load_servers


# ---------------------------------------------------------------------------
# servers.py — YAML loader
# ---------------------------------------------------------------------------


class TestLoadServers:
    def test_returns_empty_when_file_missing(self, tmp_path):
        result = load_servers(path=tmp_path / "nonexistent.yaml")
        assert result == {}

    def test_parses_valid_yaml(self, tmp_path):
        f = tmp_path / "servers.yaml"
        f.write_text(
            "servers:\n"
            "  work:\n"
            "    host: my-server.local\n"
            "    port: 8787\n"
            "    token: secret\n"
            "default: work\n"
        )
        data = load_servers(path=f)
        assert data["default"] == "work"
        assert data["servers"]["work"]["host"] == "my-server.local"

    def test_returns_empty_on_invalid_yaml(self, tmp_path):
        f = tmp_path / "servers.yaml"
        f.write_text(":\n: [\n")
        result = load_servers(path=f)
        assert result == {}

    def test_returns_empty_when_content_not_dict(self, tmp_path):
        f = tmp_path / "servers.yaml"
        f.write_text("- just a list\n")
        result = load_servers(path=f)
        assert result == {}


class TestGetServer:
    def _make_file(self, tmp_path: Path) -> Path:
        f = tmp_path / "servers.yaml"
        f.write_text(
            "servers:\n"
            "  work:\n"
            "    host: work-server\n"
            "    port: 9000\n"
            "    token: tok123\n"
            "  home:\n"
            "    host: 192.168.1.10\n"
        )
        return f

    def test_returns_server_entry(self, tmp_path):
        f = self._make_file(tmp_path)
        entry = get_server("work", path=f)
        assert entry is not None
        assert entry["host"] == "work-server"
        assert entry["port"] == "9000"
        assert entry["token"] == "tok123"

    def test_returns_none_for_unknown_name(self, tmp_path):
        f = self._make_file(tmp_path)
        assert get_server("unknown", path=f) is None

    def test_port_normalised_to_string(self, tmp_path):
        f = self._make_file(tmp_path)
        entry = get_server("work", path=f)
        assert isinstance(entry["port"], str)

    def test_partial_entry_no_token(self, tmp_path):
        f = self._make_file(tmp_path)
        entry = get_server("home", path=f)
        assert entry is not None
        assert entry["host"] == "192.168.1.10"
        assert "token" not in entry

    def test_returns_none_when_file_missing(self, tmp_path):
        assert get_server("work", path=tmp_path / "none.yaml") is None


class TestGetDefaultServer:
    def test_returns_default_server(self, tmp_path):
        f = tmp_path / "servers.yaml"
        f.write_text(
            "servers:\n"
            "  prod:\n"
            "    host: prod.example.com\n"
            "    port: 8787\n"
            "default: prod\n"
        )
        entry = get_default_server(path=f)
        assert entry is not None
        assert entry["host"] == "prod.example.com"

    def test_returns_none_when_no_default(self, tmp_path):
        f = tmp_path / "servers.yaml"
        f.write_text("servers:\n  prod:\n    host: prod.example.com\n")
        assert get_default_server(path=f) is None

    def test_returns_none_when_file_missing(self, tmp_path):
        assert get_default_server(path=tmp_path / "none.yaml") is None


# ---------------------------------------------------------------------------
# CLI flag parsing
# ---------------------------------------------------------------------------


class TestRemoteCliFlagParsing:
    """Test that --host/--port/--token/--server are accepted and parsed."""

    def _parse(self, argv):
        """Parse argv with tether.cli.main's parser; return namespace."""
        from tether.cli import main
        import argparse

        # Rebuild the parser by running main up to parse_args.
        # We use a patched _run_client / _apply_connection_args to avoid side effects.
        called = {}

        def fake_apply(args):
            called["args"] = args

        def fake_client(args):
            pass

        with patch("tether.cli._apply_connection_args", side_effect=fake_apply), \
             patch("tether.cli._run_client", side_effect=fake_client), \
             patch("tether.config.load_config"):
            try:
                main(argv)
            except SystemExit:
                pass

        return called.get("args")

    def test_host_flag(self, monkeypatch):
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        args = self._parse(["--host", "my-server", "list"])
        assert args is not None
        assert args.remote_host == "my-server"

    def test_port_flag(self, monkeypatch):
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        args = self._parse(["--port", "9000", "list"])
        assert args is not None
        assert args.remote_port == 9000

    def test_token_flag(self, monkeypatch):
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        args = self._parse(["--token", "mysecret", "list"])
        assert args is not None
        assert args.remote_token == "mysecret"

    def test_server_flag(self, monkeypatch):
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        args = self._parse(["--server", "work", "list"])
        assert args is not None
        assert args.server == "work"

    def test_short_host_flag(self, monkeypatch):
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        args = self._parse(["-H", "my-server", "list"])
        assert args is not None
        assert args.remote_host == "my-server"

    def test_short_server_flag(self, monkeypatch):
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        args = self._parse(["-S", "prod", "list"])
        assert args is not None
        assert args.server == "prod"


# ---------------------------------------------------------------------------
# _apply_connection_args — env var injection
# ---------------------------------------------------------------------------


class TestApplyConnectionArgs:
    """Test that _apply_connection_args sets the correct env vars."""

    def _make_args(self, **kwargs):
        import argparse
        ns = argparse.Namespace(
            remote_host=None,
            remote_port=None,
            remote_token=None,
            server=None,
        )
        for k, v in kwargs.items():
            setattr(ns, k, v)
        return ns

    def test_host_flag_sets_env(self, monkeypatch):
        from tether.cli import _apply_connection_args
        monkeypatch.delenv("TETHER_AGENT_HOST", raising=False)
        args = self._make_args(remote_host="my-server.local")
        with patch("tether.cli.get_default_server", return_value=None), \
             patch("tether.cli.get_server", return_value=None):
            _apply_connection_args(args)
        assert os.environ["TETHER_AGENT_HOST"] == "my-server.local"

    def test_port_flag_sets_env(self, monkeypatch):
        from tether.cli import _apply_connection_args
        monkeypatch.delenv("TETHER_AGENT_PORT", raising=False)
        args = self._make_args(remote_port=9001)
        with patch("tether.cli.get_default_server", return_value=None), \
             patch("tether.cli.get_server", return_value=None):
            _apply_connection_args(args)
        assert os.environ["TETHER_AGENT_PORT"] == "9001"

    def test_token_flag_sets_env(self, monkeypatch):
        from tether.cli import _apply_connection_args
        monkeypatch.delenv("TETHER_AGENT_TOKEN", raising=False)
        args = self._make_args(remote_token="tok_xyz")
        with patch("tether.cli.get_default_server", return_value=None), \
             patch("tether.cli.get_server", return_value=None):
            _apply_connection_args(args)
        assert os.environ["TETHER_AGENT_TOKEN"] == "tok_xyz"

    def test_server_name_applies_profile(self, monkeypatch):
        from tether.cli import _apply_connection_args
        monkeypatch.delenv("TETHER_AGENT_HOST", raising=False)
        monkeypatch.delenv("TETHER_AGENT_PORT", raising=False)
        monkeypatch.delenv("TETHER_AGENT_TOKEN", raising=False)
        args = self._make_args(server="work")
        profile = {"host": "work-server", "port": "9000", "token": "tok123"}
        with patch("tether.cli.get_server", return_value=profile):
            _apply_connection_args(args)
        assert os.environ["TETHER_AGENT_HOST"] == "work-server"
        assert os.environ["TETHER_AGENT_PORT"] == "9000"
        assert os.environ["TETHER_AGENT_TOKEN"] == "tok123"

    def test_explicit_flag_overrides_server_profile(self, monkeypatch):
        from tether.cli import _apply_connection_args
        monkeypatch.delenv("TETHER_AGENT_HOST", raising=False)
        monkeypatch.delenv("TETHER_AGENT_PORT", raising=False)
        args = self._make_args(server="work", remote_host="override-host")
        profile = {"host": "work-server", "port": "9000"}
        with patch("tether.cli.get_server", return_value=profile):
            _apply_connection_args(args)
        # Explicit --host beats server profile host
        assert os.environ["TETHER_AGENT_HOST"] == "override-host"
        # Port comes from server profile (no explicit --port)
        assert os.environ["TETHER_AGENT_PORT"] == "9000"

    def test_unknown_server_exits(self, monkeypatch, capsys):
        from tether.cli import _apply_connection_args
        args = self._make_args(server="nonexistent")
        with patch("tether.cli.get_server", return_value=None), \
             pytest.raises(SystemExit):
            _apply_connection_args(args)
        err = capsys.readouterr().err
        assert "nonexistent" in err

    def test_default_server_applied_when_no_flags(self, monkeypatch):
        from tether.cli import _apply_connection_args
        monkeypatch.delenv("TETHER_AGENT_HOST", raising=False)
        args = self._make_args()  # no flags at all
        default = {"host": "default-server", "port": "8787"}
        with patch("tether.cli.get_default_server", return_value=default):
            _apply_connection_args(args)
        assert os.environ["TETHER_AGENT_HOST"] == "default-server"

    def test_no_default_server_leaves_env_alone(self, monkeypatch):
        from tether.cli import _apply_connection_args
        monkeypatch.setenv("TETHER_AGENT_HOST", "existing-host")
        args = self._make_args()
        with patch("tether.cli.get_default_server", return_value=None):
            _apply_connection_args(args)
        # Env unchanged
        assert os.environ["TETHER_AGENT_HOST"] == "existing-host"


# ---------------------------------------------------------------------------
# tether start --host/--port still works
# ---------------------------------------------------------------------------


class TestStartBindFlags:
    """tether start --host/--port should still set the bind address."""

    def test_start_bind_host(self, monkeypatch):
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        called = {}

        def fake_start(args):
            called["bind_host"] = getattr(args, "bind_host", None)

        with patch("tether.cli._run_start", side_effect=fake_start):
            from tether.cli import main
            try:
                main(["start", "--host", "0.0.0.0"])
            except SystemExit:
                pass

        assert called.get("bind_host") == "0.0.0.0"

    def test_start_bind_port(self, monkeypatch):
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        called = {}

        def fake_start(args):
            called["bind_port"] = getattr(args, "bind_port", None)

        with patch("tether.cli._run_start", side_effect=fake_start):
            from tether.cli import main
            try:
                main(["start", "--port", "9090"])
            except SystemExit:
                pass

        assert called.get("bind_port") == 9090
