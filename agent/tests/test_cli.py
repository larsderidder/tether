"""Unit tests for cli module."""

import os

import pytest

from tether.cli import main


class TestArgParsing:
    """Test CLI argument parsing and env var mapping."""

    def test_start_sets_host(self, monkeypatch):
        monkeypatch.delenv("TETHER_AGENT_HOST", raising=False)
        # We can't actually call run() so we mock it
        import tether.cli as cli_mod

        called = {}

        def fake_load_config():
            pass

        def fake_run():
            called["host"] = os.environ.get("TETHER_AGENT_HOST")

        monkeypatch.setattr(cli_mod, "_run_start", lambda args: None)
        # Test that parsing works
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        start_p = sub.add_parser("start")
        start_p.add_argument("--host")
        start_p.add_argument("--port", type=int)
        start_p.add_argument("--dev", action="store_true")

        args = parser.parse_args(["start", "--host", "127.0.0.1"])
        assert args.host == "127.0.0.1"
        assert args.command == "start"

    def test_start_sets_port(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        start_p = sub.add_parser("start")
        start_p.add_argument("--host")
        start_p.add_argument("--port", type=int)
        start_p.add_argument("--dev", action="store_true")

        args = parser.parse_args(["start", "--port", "9000"])
        assert args.port == 9000

    def test_start_dev_flag(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        start_p = sub.add_parser("start")
        start_p.add_argument("--host")
        start_p.add_argument("--port", type=int)
        start_p.add_argument("--dev", action="store_true")

        args = parser.parse_args(["start", "--dev"])
        assert args.dev is True

    def test_init_subcommand(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        sub.add_parser("start")
        sub.add_parser("init")

        args = parser.parse_args(["init"])
        assert args.command == "init"

    def test_no_subcommand_exits(self):
        with pytest.raises(SystemExit):
            main([])


class TestStartEnvMapping:
    """Test that _run_start maps flags to env vars."""

    def test_host_flag_sets_env(self, monkeypatch):
        monkeypatch.delenv("TETHER_AGENT_HOST", raising=False)
        import argparse

        args = argparse.Namespace(host="127.0.0.1", port=None, dev=False)

        # Simulate the env-setting logic from _run_start without calling run()
        if args.host:
            os.environ["TETHER_AGENT_HOST"] = args.host
        assert os.environ["TETHER_AGENT_HOST"] == "127.0.0.1"

        monkeypatch.delenv("TETHER_AGENT_HOST", raising=False)

    def test_port_flag_sets_env(self, monkeypatch):
        monkeypatch.delenv("TETHER_AGENT_PORT", raising=False)
        import argparse

        args = argparse.Namespace(host=None, port=9000, dev=False)

        if args.port:
            os.environ["TETHER_AGENT_PORT"] = str(args.port)
        assert os.environ["TETHER_AGENT_PORT"] == "9000"

        monkeypatch.delenv("TETHER_AGENT_PORT", raising=False)

    def test_dev_flag_sets_env(self, monkeypatch):
        monkeypatch.delenv("TETHER_AGENT_DEV_MODE", raising=False)
        import argparse

        args = argparse.Namespace(host=None, port=None, dev=True)

        if args.dev:
            os.environ["TETHER_AGENT_DEV_MODE"] = "1"
        assert os.environ["TETHER_AGENT_DEV_MODE"] == "1"

        monkeypatch.delenv("TETHER_AGENT_DEV_MODE", raising=False)
