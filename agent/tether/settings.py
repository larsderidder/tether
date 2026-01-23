"""Centralized environment configuration for the Tether agent.

All environment variables are read through this module using the TETHER_AGENT_
prefix for consistency.

Usage:
    from tether.settings import settings

    if settings.dev_mode():
        ...
    port = settings.port()
"""

from __future__ import annotations

import os


def _get(name: str, default: str = "") -> str:
    """Get an environment variable value."""
    return os.environ.get(name, "").strip() or default


def _get_bool(name: str, default: bool = False) -> bool:
    """Get a boolean environment variable."""
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return value.lower() in ("1", "true", "yes")


def _get_int(name: str, default: int = 0) -> int:
    """Get an integer environment variable."""
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


class Settings:
    """Centralized settings for the Tether agent.

    Environment variables use the TETHER_AGENT_ prefix.
    """

    # -------------------------------------------------------------------------
    # Core Agent Settings
    # -------------------------------------------------------------------------

    @staticmethod
    def dev_mode() -> bool:
        """Development mode disables token requirement.

        Env: TETHER_AGENT_DEV_MODE
        """
        return _get_bool("TETHER_AGENT_DEV_MODE")

    @staticmethod
    def token() -> str:
        """Bearer token for API authentication.

        Env: TETHER_AGENT_TOKEN
        """
        return _get("TETHER_AGENT_TOKEN")

    @staticmethod
    def host() -> str:
        """Host to bind the HTTP server to.

        Env: TETHER_AGENT_HOST (default: 0.0.0.0)
        """
        return _get("TETHER_AGENT_HOST", default="0.0.0.0")

    @staticmethod
    def port() -> int:
        """Port to bind the HTTP server to.

        Env: TETHER_AGENT_PORT (default: 8787)
        """
        return _get_int("TETHER_AGENT_PORT", default=8787)

    @staticmethod
    def data_dir() -> str:
        """Directory for persistent data (sessions, logs, database).

        Env: TETHER_AGENT_DATA_DIR (default: agent/data)
        """
        value = _get("TETHER_AGENT_DATA_DIR")
        if value:
            return os.path.abspath(value)
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))

    @staticmethod
    def adapter() -> str:
        """Runner adapter selection.

        Env: TETHER_AGENT_ADAPTER (default: codex_cli)

        Options:
            - codex_cli: Legacy Codex CLI runner
            - codex_sdk_sidecar: Codex SDK sidecar
            - claude: Claude via Anthropic SDK
            - claude_local: Claude via Agent SDK (CLI OAuth)
            - claude_auto: Auto-detect (prefer OAuth, fallback to API key)
        """
        return _get("TETHER_AGENT_ADAPTER", default="codex_cli").lower()

    # -------------------------------------------------------------------------
    # Logging Settings
    # -------------------------------------------------------------------------

    @staticmethod
    def log_level() -> str:
        """Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).

        Env: TETHER_AGENT_LOG_LEVEL (default: INFO)
        """
        return _get("TETHER_AGENT_LOG_LEVEL", default="INFO").upper()

    @staticmethod
    def log_format() -> str:
        """Log format: "console" for dev-friendly, "json" for structured.

        Env: TETHER_AGENT_LOG_FORMAT (default: console)
        """
        return _get("TETHER_AGENT_LOG_FORMAT", default="console").lower()

    # -------------------------------------------------------------------------
    # Session Settings
    # -------------------------------------------------------------------------

    @staticmethod
    def session_retention_days() -> int:
        """Number of days to retain completed sessions before pruning.

        Env: TETHER_AGENT_SESSION_RETENTION_DAYS (default: 7)
        """
        return _get_int("TETHER_AGENT_SESSION_RETENTION_DAYS", default=7)

    @staticmethod
    def session_idle_timeout_seconds() -> int:
        """Seconds of inactivity before stopping a running session. 0 disables.

        Env: TETHER_AGENT_SESSION_IDLE_SECONDS (default: 0)
        """
        return _get_int("TETHER_AGENT_SESSION_IDLE_SECONDS", default=0)

    @staticmethod
    def turn_timeout_seconds() -> int:
        """Maximum seconds for a runner turn before timeout. 0 disables.

        Env: TETHER_AGENT_TURN_TIMEOUT_SECONDS (default: 0)
        """
        return _get_int("TETHER_AGENT_TURN_TIMEOUT_SECONDS", default=0)

    # -------------------------------------------------------------------------
    # Codex CLI Runner Settings
    # -------------------------------------------------------------------------

    @staticmethod
    def codex_bin() -> str:
        """Path to the Codex CLI binary.

        Env: TETHER_AGENT_CODEX_BIN (required for codex_cli adapter)
        """
        return _get("TETHER_AGENT_CODEX_BIN")

    # -------------------------------------------------------------------------
    # Claude Runner Settings
    # -------------------------------------------------------------------------

    @staticmethod
    def anthropic_api_key() -> str:
        """Anthropic API key for Claude runner.

        Env: ANTHROPIC_API_KEY (no prefix - external service credential)
        """
        return os.environ.get("ANTHROPIC_API_KEY", "").strip()

    @staticmethod
    def claude_model() -> str:
        """Claude model to use.

        Env: TETHER_AGENT_CLAUDE_MODEL (default: claude-sonnet-4-20250514)
        """
        return _get("TETHER_AGENT_CLAUDE_MODEL", default="claude-sonnet-4-20250514")

    @staticmethod
    def claude_max_tokens() -> int:
        """Maximum tokens for Claude responses.

        Env: TETHER_AGENT_CLAUDE_MAX_TOKENS (default: 4096)
        """
        return _get_int("TETHER_AGENT_CLAUDE_MAX_TOKENS", default=4096)

    # -------------------------------------------------------------------------
    # Codex SDK Sidecar Settings
    # -------------------------------------------------------------------------

    @staticmethod
    def codex_sidecar_url() -> str:
        """Base URL for the Codex SDK sidecar service.

        Env: TETHER_CODEX_SIDECAR_URL (default: http://localhost:8788)
        """
        return _get("TETHER_CODEX_SIDECAR_URL", default="http://localhost:8788")

    @staticmethod
    def codex_sidecar_token() -> str:
        """Authentication token for the sidecar service.

        Env: TETHER_CODEX_SIDECAR_TOKEN
        """
        return _get("TETHER_CODEX_SIDECAR_TOKEN")


# Singleton instance for convenient imports
settings = Settings()
