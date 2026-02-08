"""Unit tests for init_wizard module."""

import os
import stat

import pytest

from tether.init_wizard import _detect_claude_cli, _write_config
from tether.config import parse_env_file


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


class TestDetectClaudeCli:
    """Test _detect_claude_cli."""

    def test_returns_bool(self):
        result = _detect_claude_cli()
        assert isinstance(result, bool)

    def test_detects_missing_cli(self, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        assert _detect_claude_cli() is False
