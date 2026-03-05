"""Tests for the context switching feature (tether context use/list)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tether.servers import (
    get_active_context,
    get_active_context_server,
    list_contexts,
    set_active_context,
)


# ---------------------------------------------------------------------------
# get_active_context
# ---------------------------------------------------------------------------


class TestGetActiveContext:
    def test_returns_none_when_file_missing(self, tmp_path):
        result = get_active_context(context_path=tmp_path / "context")
        assert result is None

    def test_reads_context_name(self, tmp_path):
        f = tmp_path / "context"
        f.write_text("openclaw\n")
        assert get_active_context(context_path=f) == "openclaw"

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / "context"
        f.write_text("  openclaw  \n")
        assert get_active_context(context_path=f) == "openclaw"

    def test_returns_none_for_local(self, tmp_path):
        f = tmp_path / "context"
        f.write_text("local\n")
        assert get_active_context(context_path=f) is None

    def test_returns_none_for_empty_file(self, tmp_path):
        f = tmp_path / "context"
        f.write_text("")
        assert get_active_context(context_path=f) is None

    def test_returns_none_for_whitespace_only(self, tmp_path):
        f = tmp_path / "context"
        f.write_text("  \n  \n")
        assert get_active_context(context_path=f) is None


# ---------------------------------------------------------------------------
# set_active_context
# ---------------------------------------------------------------------------


class TestSetActiveContext:
    def test_writes_context_file(self, tmp_path):
        f = tmp_path / "context"
        set_active_context("openclaw", context_path=f)
        assert f.read_text().strip() == "openclaw"

    def test_clears_context_with_none(self, tmp_path):
        f = tmp_path / "context"
        f.write_text("openclaw\n")
        set_active_context(None, context_path=f)
        assert not f.exists()

    def test_clears_context_with_local(self, tmp_path):
        f = tmp_path / "context"
        f.write_text("openclaw\n")
        set_active_context("local", context_path=f)
        assert not f.exists()

    def test_clearing_nonexistent_file_is_noop(self, tmp_path):
        f = tmp_path / "context"
        set_active_context(None, context_path=f)
        assert not f.exists()

    def test_creates_parent_directories(self, tmp_path):
        f = tmp_path / "sub" / "dir" / "context"
        set_active_context("myctx", context_path=f)
        assert f.read_text().strip() == "myctx"

    def test_overwrites_existing_context(self, tmp_path):
        f = tmp_path / "context"
        set_active_context("first", context_path=f)
        set_active_context("second", context_path=f)
        assert f.read_text().strip() == "second"


# ---------------------------------------------------------------------------
# get_active_context_server
# ---------------------------------------------------------------------------


class TestGetActiveContextServer:
    def _make_servers(self, tmp_path: Path) -> Path:
        f = tmp_path / "servers.yaml"
        f.write_text(
            "servers:\n"
            "  openclaw:\n"
            "    host: openclaw-01\n"
            "    port: 8787\n"
            "    token: tok123\n"
            "  dev:\n"
            "    host: 192.168.1.50\n"
        )
        return f

    def test_returns_none_when_no_context(self, tmp_path):
        servers_f = self._make_servers(tmp_path)
        ctx_f = tmp_path / "context"
        name, profile = get_active_context_server(
            context_path=ctx_f, servers_path=servers_f
        )
        assert name is None
        assert profile is None

    def test_returns_profile_for_active_context(self, tmp_path):
        servers_f = self._make_servers(tmp_path)
        ctx_f = tmp_path / "context"
        ctx_f.write_text("openclaw\n")
        name, profile = get_active_context_server(
            context_path=ctx_f, servers_path=servers_f
        )
        assert name == "openclaw"
        assert profile is not None
        assert profile["host"] == "openclaw-01"

    def test_returns_none_profile_for_unknown_context(self, tmp_path):
        servers_f = self._make_servers(tmp_path)
        ctx_f = tmp_path / "context"
        ctx_f.write_text("nonexistent\n")
        name, profile = get_active_context_server(
            context_path=ctx_f, servers_path=servers_f
        )
        assert name == "nonexistent"
        assert profile is None


# ---------------------------------------------------------------------------
# list_contexts
# ---------------------------------------------------------------------------


class TestListContexts:
    def _make_servers(self, tmp_path: Path) -> Path:
        f = tmp_path / "servers.yaml"
        f.write_text(
            "servers:\n"
            "  openclaw:\n"
            "    host: openclaw-01\n"
            "    port: 8787\n"
            "  dev:\n"
            "    host: 192.168.1.50\n"
            "    port: 9000\n"
        )
        return f

    def test_includes_local_context(self, tmp_path):
        servers_f = self._make_servers(tmp_path)
        ctx_f = tmp_path / "context"
        result = list_contexts(servers_path=servers_f, context_path=ctx_f)
        names = [c["name"] for c in result]
        assert "local" in names

    def test_local_is_active_by_default(self, tmp_path):
        servers_f = self._make_servers(tmp_path)
        ctx_f = tmp_path / "context"
        result = list_contexts(servers_path=servers_f, context_path=ctx_f)
        local = next(c for c in result if c["name"] == "local")
        assert local["active"] == "*"

    def test_lists_all_servers(self, tmp_path):
        servers_f = self._make_servers(tmp_path)
        ctx_f = tmp_path / "context"
        result = list_contexts(servers_path=servers_f, context_path=ctx_f)
        names = [c["name"] for c in result]
        assert "openclaw" in names
        assert "dev" in names

    def test_marks_active_context(self, tmp_path):
        servers_f = self._make_servers(tmp_path)
        ctx_f = tmp_path / "context"
        ctx_f.write_text("openclaw\n")
        result = list_contexts(servers_path=servers_f, context_path=ctx_f)
        openclaw = next(c for c in result if c["name"] == "openclaw")
        local = next(c for c in result if c["name"] == "local")
        assert openclaw["active"] == "*"
        assert local["active"] == ""

    def test_empty_servers_file(self, tmp_path):
        servers_f = tmp_path / "servers.yaml"
        ctx_f = tmp_path / "context"
        result = list_contexts(servers_path=servers_f, context_path=ctx_f)
        assert len(result) == 1
        assert result[0]["name"] == "local"


# ---------------------------------------------------------------------------
# CLI integration: _apply_connection_args with active context
# ---------------------------------------------------------------------------


class TestApplyConnectionArgsWithContext:
    """Test _apply_connection_args with active context.

    Each test uses monkeypatch.setenv to pre-set env vars so monkeypatch
    can restore them at teardown, preventing leakage to other test files.
    """

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

    def _clean_env(self, monkeypatch):
        """Ensure the three connection env vars are absent, but tracked."""
        for key in ("TETHER_AGENT_HOST", "TETHER_AGENT_PORT", "TETHER_AGENT_TOKEN"):
            monkeypatch.setenv(key, "__placeholder__")
            monkeypatch.delenv(key)

    def test_active_context_applies_profile(self, monkeypatch):
        from tether.cli import _apply_connection_args

        self._clean_env(monkeypatch)

        args = self._make_args()
        profile = {"host": "ctx-server", "port": "9000", "token": "ctx-tok"}

        with patch(
            "tether.cli.get_active_context_server",
            return_value=("myctx", profile),
        ), patch("tether.cli.get_default_server", return_value=None):
            _apply_connection_args(args)

        assert os.environ["TETHER_AGENT_HOST"] == "ctx-server"
        assert os.environ["TETHER_AGENT_PORT"] == "9000"
        assert os.environ["TETHER_AGENT_TOKEN"] == "ctx-tok"

    def test_server_flag_overrides_active_context(self, monkeypatch):
        from tether.cli import _apply_connection_args

        self._clean_env(monkeypatch)

        args = self._make_args(server="explicit")
        explicit_profile = {"host": "explicit-host", "port": "7777"}

        with patch(
            "tether.cli.get_server", return_value=explicit_profile
        ), patch(
            "tether.cli.get_active_context_server",
            return_value=("myctx", {"host": "ctx-server"}),
        ):
            _apply_connection_args(args)

        assert os.environ["TETHER_AGENT_HOST"] == "explicit-host"

    def test_host_flag_overrides_active_context(self, monkeypatch):
        from tether.cli import _apply_connection_args

        self._clean_env(monkeypatch)

        args = self._make_args(remote_host="direct-host")

        # Active context should not be consulted when explicit flags are given
        with patch(
            "tether.cli.get_active_context_server",
            return_value=("myctx", {"host": "ctx-server"}),
        ), patch("tether.cli.get_default_server", return_value=None):
            _apply_connection_args(args)

        assert os.environ["TETHER_AGENT_HOST"] == "direct-host"

    def test_unknown_active_context_exits(self, monkeypatch, capsys):
        from tether.cli import _apply_connection_args

        args = self._make_args()

        with patch(
            "tether.cli.get_active_context_server",
            return_value=("gone-ctx", None),
        ), pytest.raises(SystemExit):
            _apply_connection_args(args)

        err = capsys.readouterr().err
        assert "gone-ctx" in err

    def test_no_context_falls_back_to_default(self, monkeypatch):
        from tether.cli import _apply_connection_args

        self._clean_env(monkeypatch)

        args = self._make_args()
        default = {"host": "default-srv", "port": "8787"}

        with patch(
            "tether.cli.get_active_context_server", return_value=(None, None)
        ), patch("tether.cli.get_default_server", return_value=default):
            _apply_connection_args(args)

        assert os.environ["TETHER_AGENT_HOST"] == "default-srv"


# ---------------------------------------------------------------------------
# CLI command: tether context
# ---------------------------------------------------------------------------


class TestContextCLICommands:
    def test_context_show_local(self, capsys):
        """tether context with no active context shows 'local'."""
        with patch("tether.servers.get_active_context", return_value=None):
            from tether.cli import main

            main(["context"])

        out = capsys.readouterr().out
        assert "local" in out

    def test_context_show_active(self, capsys):
        """tether context with an active context shows its name and host."""
        with patch(
            "tether.servers.get_active_context", return_value="openclaw"
        ), patch(
            "tether.servers.get_server",
            return_value={"host": "openclaw-01", "port": "8787"},
        ):
            from tether.cli import main

            main(["context"])

        out = capsys.readouterr().out
        assert "openclaw" in out
        assert "openclaw-01" in out

    def test_context_list(self, capsys):
        """tether context list shows all contexts with active marker."""
        contexts = [
            {"name": "local", "host": "127.0.0.1", "port": "8787", "active": ""},
            {
                "name": "openclaw",
                "host": "openclaw-01",
                "port": "8787",
                "active": "*",
            },
        ]
        with patch("tether.servers.list_contexts", return_value=contexts):
            from tether.cli import main

            main(["context", "list"])

        out = capsys.readouterr().out
        assert "local" in out
        assert "openclaw" in out
        assert "*" in out

    def test_context_use_sets_context(self, tmp_path, capsys):
        """tether context use <name> writes the context file."""
        ctx_f = tmp_path / "context"

        with patch(
            "tether.servers.default_context_path", return_value=ctx_f
        ), patch(
            "tether.servers.get_server",
            return_value={"host": "openclaw-01", "port": "8787"},
        ):
            from tether.cli import main

            main(["context", "use", "openclaw"])

        assert ctx_f.read_text().strip() == "openclaw"
        out = capsys.readouterr().out
        assert "openclaw" in out

    def test_context_use_local_clears_context(self, tmp_path, capsys):
        """tether context use local removes the context file."""
        ctx_f = tmp_path / "context"
        ctx_f.write_text("openclaw\n")

        with patch("tether.servers.default_context_path", return_value=ctx_f):
            from tether.cli import main

            main(["context", "use", "local"])

        assert not ctx_f.exists()
        out = capsys.readouterr().out
        assert "local" in out

    def test_context_use_unknown_exits(self, capsys):
        """tether context use <unknown> errors clearly."""
        with patch("tether.servers.get_server", return_value=None), pytest.raises(
            SystemExit
        ):
            from tether.cli import main

            main(["context", "use", "nonexistent"])

        err = capsys.readouterr().err
        assert "nonexistent" in err
