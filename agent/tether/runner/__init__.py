"""Runner selection utilities for choosing an execution backend."""

from __future__ import annotations

import json
import time
from pathlib import Path

from tether.runner.base import Runner, RunnerEvents
from tether.runner.claude import ClaudeRunner
from tether.runner.claude_local import ClaudeLocalRunner
from tether.runner.codex_cli import CodexCliRunner
from tether.runner.sidecar import SidecarRunner
from tether.settings import settings

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


def get_runner(events: RunnerEvents) -> Runner:
    """Return the configured runner adapter based on environment settings.

    Args:
        events: RunnerEvents callback sink.

    Uses TETHER_AGENT_ADAPTER to select runner. Options:
        - codex_cli: Legacy Codex CLI runner
        - codex_sdk_sidecar: Codex SDK sidecar
        - claude: Claude via Anthropic SDK (requires ANTHROPIC_API_KEY)
        - claude_local: Claude via Agent SDK (uses CLI OAuth)
        - claude_auto: Auto-detect (prefer OAuth, fallback to API key)
    """
    global _active_runner_type
    name = settings.adapter()

    if name == "codex_cli":
        runner = CodexCliRunner(events)
        _active_runner_type = runner.runner_type
        return runner

    if name == "codex_sdk_sidecar":
        runner = SidecarRunner(events)
        _active_runner_type = runner.runner_type
        return runner

    if name == "claude":
        runner = ClaudeRunner(events)
        _active_runner_type = runner.runner_type
        return runner

    if name == "claude_local":
        runner = ClaudeLocalRunner(events)
        _active_runner_type = runner.runner_type
        return runner

    if name == "claude_auto":
        # Auto-detect: prefer OAuth (no cost to user), fallback to API key
        if _has_claude_oauth():
            runner = ClaudeLocalRunner(events)
            _active_runner_type = runner.runner_type
            return runner
        if _has_anthropic_api_key():
            runner = ClaudeRunner(events)
            _active_runner_type = runner.runner_type
            return runner
        raise ValueError(
            "claude_auto: No authentication available. "
            "Either log in with 'claude' CLI or set ANTHROPIC_API_KEY."
        )

    raise ValueError(f"Unknown agent adapter: {name}")


def get_runner_type() -> str | None:
    """Return the runner type of the active runner, or None if not initialized."""
    return _active_runner_type


__all__ = ["get_runner", "get_runner_type", "Runner", "RunnerEvents"]
