"""Unit tests for init_wizard module."""

import os
import stat

import pytest

from tether.init_wizard import _configure_integrations, _write_config
from tether.config import parse_env_file


class TestConfigureIntegrations:
    """Test in-agent helper setup."""

    def test_skips_when_no_agents_detected(self):
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "tether.agent_integrations.detected_integrations", lambda: []
            )
            _configure_integrations()

    def test_installs_detected_agents_on_confirmation(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "tether.agent_integrations.detected_integrations", lambda: ["pi"]
        )
        monkeypatch.setattr("builtins.input", lambda _prompt: "1")
        monkeypatch.setattr(
            "tether.agent_integrations.install_integrations",
            lambda targets: calls.append(targets) or [],
        )

        _configure_integrations()

        assert calls == [["pi"]]


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
