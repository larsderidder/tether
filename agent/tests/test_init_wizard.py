"""Unit tests for init_wizard module."""

import os
import stat

import pytest

from tether.config import parse_env_file
from tether.init_wizard import _write_config, run_wizard
from tether.platform_defaults import (
    is_android_termux_runtime,
    recommended_default_adapter,
    recommended_default_adapter_line,
)


class TestWriteConfig:
    """Test _write_config."""

    def test_creates_file(self, tmp_path):
        dest = tmp_path / "sub" / "config.env"
        _write_config({"KEY": "value"}, dest)
        assert dest.exists()

    def test_creates_parent_dirs(self, tmp_path):
        dest = tmp_path / "a" / "b" / "config.env"
        _write_config({"K": "v"}, dest)
        assert dest.exists()

    def test_file_content_roundtrips(self, tmp_path):
        dest = tmp_path / "config.env"
        config = {"FOO": "bar", "BAZ": "hello world"}
        _write_config(config, dest)

        parsed = parse_env_file(dest)
        assert parsed["FOO"] == "bar"
        assert parsed["BAZ"] == "hello world"

    def test_file_permissions_restricted(self, tmp_path):
        dest = tmp_path / "config.env"
        _write_config({"K": "v"}, dest)

        mode = stat.S_IMODE(os.stat(dest).st_mode)
        assert mode == 0o600

    def test_quotes_values_with_hash(self, tmp_path):
        dest = tmp_path / "config.env"
        _write_config({"K": "val#ue"}, dest)

        parsed = parse_env_file(dest)
        assert parsed["K"] == "val#ue"


def test_platform_default_helpers_detect_termux() -> None:
    env = {"TERMUX_VERSION": "0.118.0"}

    assert is_android_termux_runtime(env) is True
    assert recommended_default_adapter(env) == "opencode"
    assert "recommended on Android/Termux" in recommended_default_adapter_line(env)


def test_platform_default_helpers_use_claude_off_android() -> None:
    env = {"HOME": "/tmp/demo"}

    assert is_android_termux_runtime(env) is False
    assert recommended_default_adapter(env) == "claude_auto"
    assert recommended_default_adapter_line(env).startswith(
        "TETHER_DEFAULT_AGENT_ADAPTER=claude_auto"
    )


def test_run_wizard_recommends_opencode_on_termux(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    monkeypatch.setenv("TERMUX_VERSION", "0.118.0")
    monkeypatch.setattr("tether.init_wizard.config_dir", lambda: tmp_path)
    answers = iter(["4"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    run_wizard()

    out = capsys.readouterr().out
    assert "TETHER_DEFAULT_AGENT_ADAPTER=opencode" in out


