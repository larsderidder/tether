"""Runtime-aware adapter recommendations for setup and CLI hints."""

from __future__ import annotations

import os
from collections.abc import Mapping


def is_android_termux_runtime(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the environment looks like Android or Termux.

    Example:
        >>> is_android_termux_runtime({"TERMUX_VERSION": "0.118.0"})
        True
        >>> is_android_termux_runtime({"HOME": "/tmp"})
        False
    """

    values = os.environ if env is None else env
    if values.get("TERMUX_VERSION", "").strip():
        return True
    prefix = values.get("PREFIX", "").strip()
    if "com.termux" in prefix:
        return True
    return bool(
        values.get("ANDROID_ROOT", "").strip()
        and values.get("ANDROID_DATA", "").strip()
    )


def recommended_default_adapter(env: Mapping[str, str] | None = None) -> str:
    """Return the adapter Tether should recommend in user-facing guidance.

    Example:
        >>> recommended_default_adapter({"TERMUX_VERSION": "0.118.0"})
        'opencode'
        >>> recommended_default_adapter({"HOME": "/tmp"})
        'claude_auto'
    """

    if is_android_termux_runtime(env):
        return "opencode"
    return "claude_auto"


def recommended_default_adapter_line(env: Mapping[str, str] | None = None) -> str:
    """Render the config line shown in setup output.

    Example:
        >>> recommended_default_adapter_line({"TERMUX_VERSION": "0.118.0"})
        'TETHER_DEFAULT_AGENT_ADAPTER=opencode   # recommended on Android/Termux; or claude_auto, pi_rpc, codex_sdk_sidecar'
    """

    adapter = recommended_default_adapter(env)
    if adapter == "opencode":
        return (
            "TETHER_DEFAULT_AGENT_ADAPTER=opencode   "
            "# recommended on Android/Termux; or claude_auto, pi_rpc, codex_sdk_sidecar"
        )
    return "TETHER_DEFAULT_AGENT_ADAPTER=claude_auto   # or opencode, pi_rpc, codex_sdk_sidecar"
