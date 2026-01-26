"""Unit tests for settings module."""

import os
import pytest

from tether.settings import Settings


@pytest.fixture
def clean_env(monkeypatch):
    """Remove all TETHER_AGENT_ env vars for clean tests."""
    for key in list(os.environ.keys()):
        if key.startswith("TETHER_AGENT_") or key == "ANTHROPIC_API_KEY":
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


class TestBoolSettings:
    """Test boolean environment variable parsing."""

    def test_dev_mode_default_false(self, clean_env) -> None:
        """Dev mode defaults to False."""
        assert Settings.dev_mode() is False

    def test_dev_mode_true_values(self, clean_env) -> None:
        """Dev mode accepts various true values."""
        for value in ["1", "true", "TRUE", "yes", "YES"]:
            clean_env.setenv("TETHER_AGENT_DEV_MODE", value)
            assert Settings.dev_mode() is True

    def test_dev_mode_false_values(self, clean_env) -> None:
        """Dev mode rejects non-true values."""
        for value in ["0", "false", "no", "random"]:
            clean_env.setenv("TETHER_AGENT_DEV_MODE", value)
            assert Settings.dev_mode() is False


class TestIntSettings:
    """Test integer environment variable parsing."""

    def test_port_default(self, clean_env) -> None:
        """Port defaults to 8787."""
        assert Settings.port() == 8787

    def test_port_custom(self, clean_env) -> None:
        """Port can be customized."""
        clean_env.setenv("TETHER_AGENT_PORT", "9000")
        assert Settings.port() == 9000

    def test_port_invalid_returns_default(self, clean_env) -> None:
        """Invalid port value returns default."""
        clean_env.setenv("TETHER_AGENT_PORT", "not_a_number")
        assert Settings.port() == 8787

    def test_session_retention_days(self, clean_env) -> None:
        """Session retention days setting."""
        assert Settings.session_retention_days() == 7
        clean_env.setenv("TETHER_AGENT_SESSION_RETENTION_DAYS", "30")
        assert Settings.session_retention_days() == 30

    def test_session_idle_timeout(self, clean_env) -> None:
        """Session idle timeout setting."""
        assert Settings.session_idle_timeout_seconds() == 0
        clean_env.setenv("TETHER_AGENT_SESSION_IDLE_SECONDS", "300")
        assert Settings.session_idle_timeout_seconds() == 300

    def test_turn_timeout(self, clean_env) -> None:
        """Turn timeout setting."""
        assert Settings.turn_timeout_seconds() == 0
        clean_env.setenv("TETHER_AGENT_TURN_TIMEOUT_SECONDS", "60")
        assert Settings.turn_timeout_seconds() == 60

    def test_claude_max_tokens(self, clean_env) -> None:
        """Claude max tokens setting."""
        assert Settings.claude_max_tokens() == 4096
        clean_env.setenv("TETHER_AGENT_CLAUDE_MAX_TOKENS", "8192")
        assert Settings.claude_max_tokens() == 8192


class TestStringSettings:
    """Test string environment variable parsing."""

    def test_token_default_empty(self, clean_env) -> None:
        """Token defaults to empty string."""
        assert Settings.token() == ""

    def test_token_custom(self, clean_env) -> None:
        """Token can be set."""
        clean_env.setenv("TETHER_AGENT_TOKEN", "secret123")
        assert Settings.token() == "secret123"

    def test_host_default(self, clean_env) -> None:
        """Host defaults to 0.0.0.0."""
        assert Settings.host() == "0.0.0.0"

    def test_host_custom(self, clean_env) -> None:
        """Host can be customized."""
        clean_env.setenv("TETHER_AGENT_HOST", "127.0.0.1")
        assert Settings.host() == "127.0.0.1"

    def test_adapter_default(self, clean_env) -> None:
        """Adapter defaults to codex_cli."""
        assert Settings.adapter() == "codex_cli"

    def test_adapter_custom(self, clean_env) -> None:
        """Adapter can be customized."""
        clean_env.setenv("TETHER_AGENT_ADAPTER", "CLAUDE_API")
        assert Settings.adapter() == "claude_api"  # lowercased

    def test_log_level_default(self, clean_env) -> None:
        """Log level defaults to INFO."""
        assert Settings.log_level() == "INFO"

    def test_log_level_custom(self, clean_env) -> None:
        """Log level can be customized."""
        clean_env.setenv("TETHER_AGENT_LOG_LEVEL", "debug")
        assert Settings.log_level() == "DEBUG"  # uppercased

    def test_log_format_default(self, clean_env) -> None:
        """Log format defaults to console."""
        assert Settings.log_format() == "console"

    def test_log_format_custom(self, clean_env) -> None:
        """Log format can be customized."""
        clean_env.setenv("TETHER_AGENT_LOG_FORMAT", "JSON")
        assert Settings.log_format() == "json"  # lowercased

    def test_codex_bin(self, clean_env) -> None:
        """Codex bin path setting."""
        assert Settings.codex_bin() == ""
        clean_env.setenv("TETHER_AGENT_CODEX_BIN", "/usr/local/bin/codex")
        assert Settings.codex_bin() == "/usr/local/bin/codex"

    def test_claude_model_default(self, clean_env) -> None:
        """Claude model defaults to sonnet."""
        assert Settings.claude_model() == "claude-sonnet-4-20250514"

    def test_claude_model_custom(self, clean_env) -> None:
        """Claude model can be customized."""
        clean_env.setenv("TETHER_AGENT_CLAUDE_MODEL", "claude-opus-4")
        assert Settings.claude_model() == "claude-opus-4"

    def test_anthropic_api_key(self, clean_env) -> None:
        """Anthropic API key uses no prefix."""
        assert Settings.anthropic_api_key() == ""
        clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
        assert Settings.anthropic_api_key() == "sk-ant-xxx"

    def test_codex_sidecar_url_default(self, clean_env) -> None:
        """Sidecar URL has default."""
        assert Settings.codex_sidecar_url() == "http://localhost:8788"

    def test_codex_sidecar_token(self, clean_env) -> None:
        """Sidecar token setting."""
        assert Settings.codex_sidecar_token() == ""
        clean_env.setenv("TETHER_CODEX_SIDECAR_TOKEN", "token123")
        assert Settings.codex_sidecar_token() == "token123"


class TestDataDir:
    """Test data directory setting."""

    def test_data_dir_default(self, clean_env) -> None:
        """Data dir defaults to agent/data."""
        result = Settings.data_dir()
        assert result.endswith("data")
        assert os.path.isabs(result)

    def test_data_dir_custom(self, clean_env) -> None:
        """Data dir can be customized."""
        clean_env.setenv("TETHER_AGENT_DATA_DIR", "/tmp/tether-data")
        assert Settings.data_dir() == "/tmp/tether-data"

    def test_data_dir_relative_made_absolute(self, clean_env) -> None:
        """Relative data dir is made absolute."""
        clean_env.setenv("TETHER_AGENT_DATA_DIR", "relative/path")
        result = Settings.data_dir()
        assert os.path.isabs(result)
        assert result.endswith("relative/path")
