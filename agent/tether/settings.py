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

        Env: TETHER_AGENT_DATA_DIR

        Default depends on context:
            - Source checkout (pyproject.toml exists): ``agent/data/``
            - Installed package: ``~/.local/share/tether/`` (XDG_DATA_HOME)
        """
        value = _get("TETHER_AGENT_DATA_DIR")
        if value:
            return os.path.abspath(value)

        # Detect source checkout: pyproject.toml lives one level above the package
        package_parent = os.path.join(os.path.dirname(__file__), "..")
        if os.path.isfile(os.path.join(package_parent, "pyproject.toml")):
            return os.path.abspath(os.path.join(package_parent, "data"))

        # Installed package — use XDG data directory
        from tether.config import data_dir_default

        path = data_dir_default()
        return str(path)

    @staticmethod
    def adapter() -> str:
        """Runner adapter selection.

        Env: TETHER_AGENT_ADAPTER (default: claude_auto)

        Options:
            - codex_sdk_sidecar: Codex SDK sidecar
            - claude_api: Claude via Anthropic SDK (requires API key)
            - claude_subprocess: Claude via Agent SDK in subprocess (CLI OAuth)
            - claude_auto: Auto-detect (prefer OAuth, fallback to API key)
            - litellm: Any model via LiteLLM (DeepSeek, Kimi, Gemini, etc.)
        """
        return _get("TETHER_AGENT_ADAPTER", default="claude_auto").lower()

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

    @staticmethod
    def log_file() -> str:
        """Path to an optional log file. Empty means no file logging.

        Env: TETHER_AGENT_LOG_FILE
        """
        return _get("TETHER_AGENT_LOG_FILE")

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
    def bridge_error_debounce_seconds() -> int:
        """Debounce error notifications sent by messaging bridges.

        When a runner hits an error, multiple error events/status changes may be
        emitted in quick succession. Bridges can use this setting to avoid
        spamming messaging channels.

        Env: TETHER_AGENT_BRIDGE_ERROR_DEBOUNCE_SECONDS (default: 30)
        """
        return _get_int("TETHER_AGENT_BRIDGE_ERROR_DEBOUNCE_SECONDS", default=30)

    @staticmethod
    def turn_timeout_seconds() -> int:
        """Maximum seconds for a runner turn before timeout. 0 disables.

        Env: TETHER_AGENT_TURN_TIMEOUT_SECONDS (default: 0)
        """
        return _get_int("TETHER_AGENT_TURN_TIMEOUT_SECONDS", default=0)

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

    # -------------------------------------------------------------------------
    # LiteLLM Runner Settings
    # -------------------------------------------------------------------------

    @staticmethod
    def litellm_model() -> str:
        """LiteLLM model identifier.

        Uses LiteLLM model naming: provider/model (e.g. openrouter/deepseek/deepseek-chat,
        deepseek/deepseek-chat, gemini/gemini-2.0-flash, etc.)

        Env: TETHER_AGENT_LITELLM_MODEL (default: openrouter/deepseek/deepseek-chat)
        """
        return _get("TETHER_AGENT_LITELLM_MODEL", default="openrouter/deepseek/deepseek-chat")

    @staticmethod
    def litellm_max_tokens() -> int:
        """Maximum tokens for LiteLLM responses.

        Env: TETHER_AGENT_LITELLM_MAX_TOKENS (default: 4096)
        """
        return _get_int("TETHER_AGENT_LITELLM_MAX_TOKENS", default=4096)

    # -------------------------------------------------------------------------
    # Bridge Settings (Messaging Platforms)
    # -------------------------------------------------------------------------

    @staticmethod
    def telegram_bot_token() -> str:
        """Telegram bot token for bridge integration.

        Env: TELEGRAM_BOT_TOKEN (no prefix - external service credential)
        """
        return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

    @staticmethod
    def telegram_group_id() -> int:
        """Telegram forum group ID for creating topics.

        Env: TELEGRAM_FORUM_GROUP_ID (preferred), TELEGRAM_GROUP_ID (legacy)
        """
        value = os.environ.get("TELEGRAM_FORUM_GROUP_ID", "").strip() or os.environ.get(
            "TELEGRAM_GROUP_ID", ""
        ).strip()
        if not value:
            return 0
        try:
            return int(value)
        except ValueError:
            return 0

    @staticmethod
    def slack_bot_token() -> str:
        """Slack bot token for bridge integration.

        Env: SLACK_BOT_TOKEN (no prefix - external service credential)
        """
        return os.environ.get("SLACK_BOT_TOKEN", "").strip()

    @staticmethod
    def slack_app_token() -> str:
        """Slack app-level token for socket mode.

        Env: SLACK_APP_TOKEN (no prefix - external service credential)
        """
        return os.environ.get("SLACK_APP_TOKEN", "").strip()

    @staticmethod
    def slack_channel_id() -> str:
        """Slack channel ID for posting messages.

        Env: SLACK_CHANNEL_ID
        """
        return os.environ.get("SLACK_CHANNEL_ID", "").strip()

    @staticmethod
    def discord_bot_token() -> str:
        """Discord bot token for bridge integration.

        Env: DISCORD_BOT_TOKEN (no prefix - external service credential)
        """
        return os.environ.get("DISCORD_BOT_TOKEN", "").strip()

    @staticmethod
    def discord_channel_id() -> int:
        """Discord channel ID for creating threads.

        Env: DISCORD_CHANNEL_ID
        """
        value = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
        if not value:
            return 0
        try:
            return int(value)
        except ValueError:
            return 0

    @staticmethod
    def discord_require_pairing() -> bool:
        """Require Discord users to pair before using the bot.

        When enabled, only paired users (or explicitly allowlisted users) may run
        commands or send session input.

        Env: DISCORD_REQUIRE_PAIRING (default: 0)
        """
        return _get_bool("DISCORD_REQUIRE_PAIRING", default=False)

    @staticmethod
    def discord_pairing_code() -> str:
        """Optional fixed pairing code for Discord.

        If unset and pairing is required, the Discord bridge will generate a code
        on startup and log it.

        Env: DISCORD_PAIRING_CODE
        """
        return os.environ.get("DISCORD_PAIRING_CODE", "").strip()

    @staticmethod
    def discord_allowed_user_ids() -> set[int]:
        """Comma-separated Discord user IDs that are always authorized.

        Env: DISCORD_ALLOWED_USER_IDS (e.g. "123,456")
        """
        raw = os.environ.get("DISCORD_ALLOWED_USER_IDS", "").strip()
        if not raw:
            return set()
        out: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.add(int(part))
            except ValueError:
                continue
        return out


# Singleton instance for convenient imports
settings = Settings()
