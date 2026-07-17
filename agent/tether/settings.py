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


def _get_float(name: str, default: float = 0.0) -> float:
    """Get a float environment variable."""
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _get_bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Get an integer environment variable clamped to a safe range."""
    value = _get_int(name, default)
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _get_bounded_float(
    name: str, default: float, *, minimum: float, maximum: float
) -> float:
    """Get a float environment variable clamped to a safe range."""
    value = _get_float(name, default)
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _get_int_set(name: str) -> set[int]:
    """Parse a comma-separated list of integer IDs."""
    raw = os.environ.get(name, "").strip()
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


class Settings:
    """Centralized settings for the Tether agent.

    Environment variables use the TETHER_AGENT_ prefix.
    """

    # -------------------------------------------------------------------------
    # Tool Output Settings
    # -------------------------------------------------------------------------

    @staticmethod
    def pi_resume_max_session_file_bytes() -> int:
        """Maximum pi session file size Tether will try to resume.

        Env: TETHER_PI_RESUME_MAX_SESSION_FILE_BYTES (default: 150 MB)
        """
        return _get_bounded_int(
            "TETHER_PI_RESUME_MAX_SESSION_FILE_BYTES",
            150 * 1024 * 1024,
            minimum=10 * 1024 * 1024,
            maximum=1024 * 1024 * 1024,
        )

    @staticmethod
    def pi_tool_output_max_chars() -> int:
        """Maximum characters kept from pi tool output in Tether events.

        Env: TETHER_PI_TOOL_OUTPUT_MAX_CHARS (default: 1200)
        """
        return _get_bounded_int(
            "TETHER_PI_TOOL_OUTPUT_MAX_CHARS",
            1200,
            minimum=200,
            maximum=20000,
        )

    @staticmethod
    def pi_tool_output_max_lines() -> int:
        """Maximum lines kept from pi tool output in Tether events.

        Env: TETHER_PI_TOOL_OUTPUT_MAX_LINES (default: 80)
        """
        return _get_bounded_int(
            "TETHER_PI_TOOL_OUTPUT_MAX_LINES",
            80,
            minimum=5,
            maximum=1000,
        )

    @staticmethod
    def bridge_tool_output_inline_chars() -> int:
        """Maximum characters shown inline for bridge tool output.

        Env: TETHER_BRIDGE_TOOL_OUTPUT_INLINE_CHARS (default: 800)
        """
        return _get_bounded_int(
            "TETHER_BRIDGE_TOOL_OUTPUT_INLINE_CHARS",
            800,
            minimum=100,
            maximum=1800,
        )

    @staticmethod
    def bridge_tool_output_inline_lines() -> int:
        """Maximum lines shown inline for bridge tool output.

        Env: TETHER_BRIDGE_TOOL_OUTPUT_INLINE_LINES (default: 6)
        """
        return _get_bounded_int(
            "TETHER_BRIDGE_TOOL_OUTPUT_INLINE_LINES",
            6,
            minimum=1,
            maximum=50,
        )

    @staticmethod
    def bridge_output_flush_delay_seconds() -> float:
        """Seconds to buffer non-final bridge output before sending.

        Env: TETHER_BRIDGE_OUTPUT_FLUSH_DELAY_SECONDS (default: 2)
        """
        return _get_bounded_float(
            "TETHER_BRIDGE_OUTPUT_FLUSH_DELAY_SECONDS",
            2.0,
            minimum=0.0,
            maximum=300.0,
        )

    @staticmethod
    def bridge_tool_activity_flush_delay_seconds() -> float:
        """Seconds to buffer bridge tool activity before sending a bundle.

        Env: TETHER_BRIDGE_TOOL_ACTIVITY_FLUSH_DELAY_SECONDS (default: 5)
        """
        return _get_bounded_float(
            "TETHER_BRIDGE_TOOL_ACTIVITY_FLUSH_DELAY_SECONDS",
            5.0,
            minimum=0.0,
            maximum=300.0,
        )

    @staticmethod
    def bridge_tool_activity_combine_messages() -> bool:
        """Render buffered bridge tool activity as one platform message when possible.

        Env: TETHER_BRIDGE_TOOL_ACTIVITY_COMBINE_MESSAGES (default: 1)
        """
        return _get_bool(
            "TETHER_BRIDGE_TOOL_ACTIVITY_COMBINE_MESSAGES",
            default=True,
        )

    @staticmethod
    def bridge_tool_activity_flush_on_final_only() -> bool:
        """Keep bridge tool activity buffered until final output or turn end.

        Env: TETHER_BRIDGE_TOOL_ACTIVITY_FLUSH_ON_FINAL_ONLY (default: 0)
        """
        return _get_bool(
            "TETHER_BRIDGE_TOOL_ACTIVITY_FLUSH_ON_FINAL_ONLY",
            default=False,
        )

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
    def adapter() -> str | None:
        """Default runner adapter.

        Env: TETHER_DEFAULT_AGENT_ADAPTER
        Backwards compat: TETHER_AGENT_ADAPTER is still accepted if the new name is not set.

        Returns None when not configured. Callers that need a concrete adapter
        must handle None and surface a clear error rather than silently falling
        back to a default.

        Options:
            - claude_auto: Auto-detect Claude (requires OAuth or ANTHROPIC_API_KEY)
            - claude_subprocess: Claude via Agent SDK subprocess
            - opencode: OpenCode via sidecar
            - codex_sdk_sidecar: Codex via SDK sidecar
            - pi_rpc: Pi coding agent via JSON-RPC subprocess
            - litellm: Any model via LiteLLM (DeepSeek, Kimi, Gemini, etc.)
        """
        value = _get("TETHER_DEFAULT_AGENT_ADAPTER") or _get("TETHER_AGENT_ADAPTER")
        return value.lower() if value else None

    @staticmethod
    def adapter_model_key(adapter: str | None) -> str:
        """Return the environment key stem for an adapter's model settings."""
        raw = str(adapter or "").strip().lower()
        if raw in {"claude", "claude_auto", "claude_subprocess", "claude_api"}:
            return "CLAUDE"
        if raw in {"codex", "codex_sdk_sidecar"}:
            return "CODEX"
        if raw in {"pi", "pi_rpc"}:
            return "PI"
        if raw in {"opencode", "opencode_sdk_sidecar"}:
            return "OPENCODE"
        if raw == "litellm":
            return "LITELLM"
        return "".join(ch if ch.isalnum() else "_" for ch in raw.upper()).strip("_")

    @staticmethod
    def adapter_default_model(adapter: str | None) -> str:
        """Default model for new sessions created with an adapter.

        Env: TETHER_<ADAPTER>_DEFAULT_MODEL, with runner-specific legacy
        settings used as fallbacks where they already exist.
        """
        key = Settings.adapter_model_key(adapter)
        if key:
            value = _get(f"TETHER_{key}_DEFAULT_MODEL")
            if value:
                return value
        if key == "CLAUDE":
            return Settings.claude_model()
        if key == "CODEX":
            return Settings.codex_sidecar_model()
        if key == "LITELLM":
            return Settings.litellm_model()
        return ""

    @staticmethod
    def adapter_models(adapter: str | None) -> list[str]:
        """Configured model choices for an adapter.

        Env: TETHER_<ADAPTER>_MODELS, comma-separated.
        """
        key = Settings.adapter_model_key(adapter)
        raw = _get(f"TETHER_{key}_MODELS") if key else ""
        models = [item.strip() for item in raw.split(",") if item.strip()]
        default = Settings.adapter_default_model(adapter)
        if default:
            models = [item for item in models if item != default]
            models.insert(0, default)
        return models

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
    def bridge_reaction_new_session_enabled() -> bool:
        """Enable the `!new` plus checkmark reaction shortcut in Slack/Discord.

        When enabled, a top-level control-channel message whose first line starts
        with ``!new`` can create and start a new session when reacted to with the
        configured emoji.

        Env: TETHER_BRIDGE_REACTION_NEW_SESSION_ENABLED (default: 1)
        """
        return _get_bool("TETHER_BRIDGE_REACTION_NEW_SESSION_ENABLED", default=True)

    @staticmethod
    def bridge_reaction_new_session_emoji() -> str:
        """Emoji or reaction name used to trigger the new-session shortcut.

        Env: TETHER_BRIDGE_REACTION_NEW_SESSION_EMOJI (default: ✅)
        """
        return _get("TETHER_BRIDGE_REACTION_NEW_SESSION_EMOJI", default="✅")

    @staticmethod
    def bridge_reaction_new_session_allow_plain_messages() -> bool:
        """Allow plain reacted control-channel messages to create new sessions.

        When enabled, a top-level reacted control-channel message that does not
        start with ``!`` uses its full text as the initial prompt. The session
        runs in the Tether server's current working directory and uses the
        configured default adapter.

        Env: TETHER_BRIDGE_REACTION_NEW_SESSION_ALLOW_PLAIN_MESSAGES (default: 0)
        """
        return _get_bool(
            "TETHER_BRIDGE_REACTION_NEW_SESSION_ALLOW_PLAIN_MESSAGES",
            default=False,
        )

    @staticmethod
    def debug_attach_logs() -> bool:
        """Attach diagnostic text files for bridge error delivery.

        When enabled, Slack and Discord error notifications upload a diagnostic
        bundle instead of emitting only a plain text status message.

        Env: TETHER_DEBUG_ATTACH_LOGS (default: 1)
        """
        return _get_bool("TETHER_DEBUG_ATTACH_LOGS", default=True)

    @staticmethod
    def external_sync_watcher_enabled() -> bool:
        """Enable polling sync for platform-bound external sessions.

        Env: TETHER_EXTERNAL_SYNC_WATCHER_ENABLED (default: 1)
        """
        return _get_bool("TETHER_EXTERNAL_SYNC_WATCHER_ENABLED", default=True)

    @staticmethod
    def external_sync_interval_seconds() -> float:
        """Polling interval for external session sync.

        Env: TETHER_EXTERNAL_SYNC_INTERVAL_SECONDS (default: 3)
        """
        return _get_bounded_float(
            "TETHER_EXTERNAL_SYNC_INTERVAL_SECONDS",
            3.0,
            minimum=1.0,
            maximum=60.0,
        )

    @staticmethod
    def external_sync_initial_lookback_seconds() -> float:
        """Maximum initial watcher catch-up window when the cursor is missing.

        Env: TETHER_EXTERNAL_SYNC_INITIAL_LOOKBACK_SECONDS (default: 3600)
        """
        return _get_bounded_float(
            "TETHER_EXTERNAL_SYNC_INITIAL_LOOKBACK_SECONDS",
            3600.0,
            minimum=0.0,
            maximum=86400.0,
        )

    @staticmethod
    def telegram_output_max_messages() -> int:
        """Maximum Telegram messages to send for one bridge output.

        Env: TETHER_TELEGRAM_OUTPUT_MAX_MESSAGES (default: 0, unlimited)
        """
        return int(
            _get_bounded_float(
                "TETHER_TELEGRAM_OUTPUT_MAX_MESSAGES",
                0.0,
                minimum=0.0,
                maximum=200.0,
            )
        )

    @staticmethod
    def git_auto_checkpoint() -> bool:
        """Auto-commit dirty git worktrees after each completed turn.

        Env: TETHER_GIT_AUTO_CHECKPOINT (default: 0)
        """
        return _get_bool("TETHER_GIT_AUTO_CHECKPOINT", default=False)

    @staticmethod
    def git_auto_branch() -> bool:
        """Auto-create a working branch for cloned workspaces.

        Env: TETHER_GIT_AUTO_BRANCH (default: 0)
        """
        return _get_bool("TETHER_GIT_AUTO_BRANCH", default=False)

    @staticmethod
    def git_branch_pattern() -> str:
        """Branch name pattern for auto-created working branches.

        Env: TETHER_GIT_BRANCH_PATTERN (default: tether/{session_id})
        """
        return _get("TETHER_GIT_BRANCH_PATTERN", default="tether/{session_id}")

    @staticmethod
    def git_fetch_cache_seconds() -> int:
        """Per-repo fetch cache TTL in seconds.

        Env: TETHER_GIT_FETCH_CACHE_SECONDS (default: 300)
        """
        return _get_int("TETHER_GIT_FETCH_CACHE_SECONDS", default=300)

    @staticmethod
    def git_fetch_timeout() -> int:
        """Timeout for `git fetch origin` in seconds.

        Env: TETHER_GIT_FETCH_TIMEOUT (default: 30)
        """
        return _get_int("TETHER_GIT_FETCH_TIMEOUT", default=30)

    @staticmethod
    def repo_retention_days() -> int:
        """Days to keep unused shared repo clones before pruning.

        Env: TETHER_REPO_RETENTION_DAYS (default: 30)
        """
        return _get_int("TETHER_REPO_RETENTION_DAYS", default=30)

    @staticmethod
    def workspace_max_disk_gb() -> float | None:
        """Optional workspace disk usage warning threshold in gigabytes.

        Env: TETHER_WORKSPACE_MAX_DISK_GB
        """
        value = os.environ.get("TETHER_WORKSPACE_MAX_DISK_GB", "").strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    def turn_timeout_seconds() -> int:
        """Maximum seconds for a runner turn before timeout. 0 disables.

        Env: TETHER_AGENT_TURN_TIMEOUT_SECONDS (default: 0)
        """
        return _get_int("TETHER_AGENT_TURN_TIMEOUT_SECONDS", default=0)

    # -------------------------------------------------------------------------
    # SSH Access Settings
    # -------------------------------------------------------------------------

    @staticmethod
    def ssh_enabled() -> bool:
        """Enable the optional SSH control server.

        Env: TETHER_SSH_ENABLED (default: 0)
        """
        return _get_bool("TETHER_SSH_ENABLED", default=False)

    @staticmethod
    def ssh_host() -> str:
        """Host to bind the SSH control server to.

        Env: TETHER_SSH_HOST (default: 0.0.0.0)
        """
        return _get("TETHER_SSH_HOST", default="0.0.0.0")

    @staticmethod
    def ssh_port() -> int:
        """Port to bind the SSH control server to.

        Env: TETHER_SSH_PORT (default: 8822)
        """
        return _get_int("TETHER_SSH_PORT", default=8822)

    @staticmethod
    def ssh_host_key_path() -> str:
        """Path to the SSH host private key.

        Env: TETHER_SSH_HOST_KEY_PATH
        Default: <data_dir>/ssh_host_ed25519_key
        """
        configured = _get("TETHER_SSH_HOST_KEY_PATH")
        if configured:
            return os.path.abspath(configured)
        return os.path.join(settings.data_dir(), "ssh_host_ed25519_key")

    @staticmethod
    def ssh_authorized_keys_path() -> str:
        """Path to the authorized client public keys file.

        Env: TETHER_SSH_AUTHORIZED_KEYS_PATH
        Default: <data_dir>/ssh_authorized_keys
        """
        configured = _get("TETHER_SSH_AUTHORIZED_KEYS_PATH")
        if configured:
            return os.path.abspath(configured)
        return os.path.join(settings.data_dir(), "ssh_authorized_keys")

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

    @staticmethod
    def codex_sidecar_codex_bin() -> str:
        """Optional Codex CLI path used by the direct runner fallback.

        Env: TETHER_CODEX_SIDECAR_CODEX_BIN
        """
        return _get("TETHER_CODEX_SIDECAR_CODEX_BIN")

    @staticmethod
    def codex_sidecar_model() -> str:
        """Optional model override for sidecar or CLI-backed Codex turns.

        Env: TETHER_CODEX_SIDECAR_MODEL
        """
        return _get("TETHER_CODEX_SIDECAR_MODEL")

    @staticmethod
    def codex_sidecar_sandbox_mode() -> str:
        """Optional sandbox mode override for sidecar or CLI-backed Codex turns.

        Env: TETHER_CODEX_SIDECAR_SANDBOX_MODE
        """
        return _get("TETHER_CODEX_SIDECAR_SANDBOX_MODE")

    @staticmethod
    def codex_sidecar_approval_policy() -> str:
        """Optional approval policy override for sidecar or CLI-backed Codex turns.

        Env: TETHER_CODEX_SIDECAR_APPROVAL_POLICY
        """
        return _get("TETHER_CODEX_SIDECAR_APPROVAL_POLICY")

    @staticmethod
    def opencode_sidecar_url() -> str:
        """Base URL for the OpenCode sidecar service.

        Env: TETHER_OPENCODE_SIDECAR_URL (default: http://localhost:8790)
        """
        return _get("TETHER_OPENCODE_SIDECAR_URL", default="http://localhost:8790")

    @staticmethod
    def opencode_sidecar_token() -> str:
        """Authentication token for the OpenCode sidecar service.

        Env: TETHER_OPENCODE_SIDECAR_TOKEN
        """
        return _get("TETHER_OPENCODE_SIDECAR_TOKEN")

    @staticmethod
    def opencode_sidecar_managed() -> bool:
        """Whether Tether should auto-manage the OpenCode sidecar process.

        Env: TETHER_OPENCODE_SIDECAR_MANAGED (default: 1)
        """
        return _get_bool("TETHER_OPENCODE_SIDECAR_MANAGED", default=True)

    @staticmethod
    def opencode_sidecar_cmd() -> str:
        """Command used when managed OpenCode sidecar is enabled.

        Env: TETHER_OPENCODE_SIDECAR_CMD (default: "")
        When empty (the default), the manager uses the bundled sidecar JS
        shipped with the package, falling back to the source tree for dev.
        Set this to override with a custom command.
        """
        return _get("TETHER_OPENCODE_SIDECAR_CMD", default="")

    @staticmethod
    def opencode_sidecar_startup_timeout_seconds() -> int:
        """Seconds to wait for managed OpenCode sidecar health.

        Env: TETHER_OPENCODE_SIDECAR_STARTUP_TIMEOUT_SECONDS (default: 15)
        """
        return _get_int("TETHER_OPENCODE_SIDECAR_STARTUP_TIMEOUT_SECONDS", default=15)

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
        return _get(
            "TETHER_AGENT_LITELLM_MODEL", default="openrouter/deepseek/deepseek-chat"
        )

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
        value = (
            os.environ.get("TELEGRAM_FORUM_GROUP_ID", "").strip()
            or os.environ.get("TELEGRAM_GROUP_ID", "").strip()
        )
        if not value:
            return 0
        try:
            return int(value)
        except ValueError:
            return 0

    @staticmethod
    def telegram_allowed_user_ids() -> set[int]:
        """Comma-separated Telegram user IDs allowed to control the bridge.

        Env: TELEGRAM_ALLOWED_USER_IDS (for example, "123,456")
        """
        return _get_int_set("TELEGRAM_ALLOWED_USER_IDS")

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
    def discord_guild_id() -> int:
        """Discord guild ID used for automatic control-channel bootstrap.

        Env: DISCORD_GUILD_ID
        """
        value = os.environ.get("DISCORD_GUILD_ID", "").strip()
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
        return _get_int_set("DISCORD_ALLOWED_USER_IDS")

    @staticmethod
    def discord_auto_pair_user_ids() -> set[int]:
        """Comma-separated Discord user IDs to pre-authorize as paired.

        Env: DISCORD_AUTO_PAIR_USER_IDS (e.g. "123,456")
        """
        return _get_int_set("DISCORD_AUTO_PAIR_USER_IDS")


# Singleton instance for convenient imports
settings = Settings()
