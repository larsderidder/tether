"""Unit tests for init_wizard module."""

import os
import stat

import pytest

from tether.init_wizard import _detect_adapter, _write_config
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


class TestDetectAdapter:
    """Test _detect_adapter."""

    def test_returns_default_when_nothing_found(self, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        # No agent CLIs on PATH — should return claude_auto as fallback
        result = _detect_adapter()
        assert result == "claude_auto"

    def test_detects_claude(self, monkeypatch, tmp_path):
        fake_bin = tmp_path / "claude"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        result = _detect_adapter()
        assert result == "claude_auto"

    def test_detects_opencode(self, monkeypatch, tmp_path):
        fake_bin = tmp_path / "opencode"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        result = _detect_adapter()
        assert result == "opencode"
