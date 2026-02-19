"""Runner selection utilities for choosing an execution backend."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from tether.runner.base import Runner, RunnerEvents
from tether.settings import settings

# Lazy imports - these SDKs are heavy and slow down startup
if TYPE_CHECKING:
    from tether.runner.claude_subprocess import ClaudeSubprocessRunner
    from tether.runner.codex_sdk_sidecar import SidecarRunner
    from tether.runner.litellm_runner import LiteLLMRunner
    from tether.runner.opencode_sdk_sidecar import OpenCodeSidecarRunner
    from tether.runner.pi_rpc import PiRpcRunner

# Cache the runner type after first initialization
_active_runner_type: str | None = None


def _has_anthropic_api_key() -> bool:
    """Check if ANTHROPIC_API_KEY is set."""
    return bool(settings.anthropic_api_key())


def _has_claude_oauth() -> bool:
    """Check if Claude CLI OAuth credentials exist."""
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if not creds_path.exists():
        return False
    try:
        with open(creds_path) as f:
            creds = json.load(f)
        oauth = creds.get("claudeAiOauth", {})
        # Check if token exists and hasn't expired
        access_token = oauth.get("accessToken")
        expires_at = oauth.get("expiresAt", 0)
        if not access_token:
            return False
        # Check expiry (expires_at is in milliseconds)
        if expires_at and expires_at / 1000 < time.time():
            return False
        return True
    except Exception:
        return False


def _require_claude_sdk() -> None:
    """Verify the Claude Agent SDK is installed."""
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError as e:
        raise ValueError(
            "Claude adapter requires claude-agent-sdk. "
            "Install it with: pip install claude-agent-sdk"
        ) from e


def get_runner(events: RunnerEvents) -> Runner:
    """Return the configured runner adapter based on environment settings.

    Args:
        events: RunnerEvents callback sink.

    Uses TETHER_DEFAULT_AGENT_ADAPTER to select runner. Options:
        - codex_sdk_sidecar: Codex SDK sidecar
        - claude_subprocess: Claude via Agent SDK in subprocess (OAuth or API key)
        - claude_auto: Auto-detect (requires OAuth or ANTHROPIC_API_KEY)
        - litellm: Any model via LiteLLM (DeepSeek, Kimi, Gemini, etc.)
        - opencode: OpenCode sidecar
        - pi_rpc: Pi coding agent via JSON-RPC subprocess

    Runners are imported lazily to speed up agent startup.
    """
    global _active_runner_type
    name = settings.adapter()

    if name == "codex_sdk_sidecar":
        from tether.runner.codex_sdk_sidecar import SidecarRunner

        runner = SidecarRunner(events)
        _active_runner_type = runner.runner_type
        return runner

    if name in ("claude_subprocess", "claude_api"):
        # claude_api is accepted as an alias for backwards compatibility
        _require_claude_sdk()
        from tether.runner.claude_subprocess import ClaudeSubprocessRunner

        runner = ClaudeSubprocessRunner(events)
        _active_runner_type = runner.runner_type
        return runner

    if name == "claude_auto":
        # Auto-detect: need either OAuth or API key, plus the SDK
        if _has_claude_oauth() or _has_anthropic_api_key():
            _require_claude_sdk()
            from tether.runner.claude_subprocess import ClaudeSubprocessRunner

            runner = ClaudeSubprocessRunner(events)
            _active_runner_type = runner.runner_type
            return runner
        raise ValueError(
            "claude_auto: No authentication available. "
            "Either log in with 'claude' CLI or set ANTHROPIC_API_KEY."
        )

    if name == "litellm":
        try:
            import litellm as _litellm  # noqa: F401 — verify installed
        except ImportError as e:
            raise ValueError(
                "litellm adapter requires litellm. "
                "Install with: pip install tether-ai[litellm]"
            ) from e

        from tether.runner.litellm_runner import LiteLLMRunner

        runner = LiteLLMRunner(events)
        _active_runner_type = runner.runner_type
        return runner

    if name == "opencode":
        from tether.runner.opencode_sdk_sidecar import OpenCodeSidecarRunner

        runner = OpenCodeSidecarRunner(events)
        _active_runner_type = runner.runner_type
        return runner

    if name == "pi_rpc":
        from tether.runner.pi_rpc import PiRpcRunner

        runner = PiRpcRunner(events)
        _active_runner_type = runner.runner_type
        return runner

    raise ValueError(f"Unknown agent adapter: {name}")


def get_runner_type() -> str | None:
    """Return the runner type of the active runner, or None if not initialized."""
    return _active_runner_type


__all__ = ["get_runner", "get_runner_type", "Runner", "RunnerEvents"]
