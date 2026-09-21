"""Canonical runner adapter names and user-facing aliases."""

from __future__ import annotations

_ADAPTER_ALIASES: dict[str, str] = {
    "claude": "claude_auto",
    "codex": "codex_sdk_sidecar",
    "opencode_sdk_sidecar": "opencode",
    "pi": "pi_rpc",
    "script": "automation",
}

SUPPORTED_ADAPTERS = frozenset(
    {
        "automation",
        "claude_api",
        "claude_auto",
        "claude_subprocess",
        "codex_sdk_sidecar",
        "litellm",
        "opencode",
        "pi_rpc",
    }
)

_ADAPTER_LABELS: dict[str, str] = {
    "automation": "Automation",
    "claude_api": "Claude API",
    "claude_auto": "Claude",
    "claude_subprocess": "Claude",
    "codex_sdk_sidecar": "Codex",
    "litellm": "LiteLLM",
    "opencode": "OpenCode",
    "pi_rpc": "Pi",
}


def normalize_adapter_name(name: str | None) -> str | None:
    """Return the canonical adapter name for a configured name or alias."""
    if name is None:
        return None
    normalized = name.strip().lower()
    if not normalized:
        return None
    return _ADAPTER_ALIASES.get(normalized, normalized)


def bridge_agent_to_adapter(name: str) -> str | None:
    """Map a bridge agent argument to an allowed canonical adapter."""
    # ASVS 2.2.1: bridge input is accepted only from the adapter allowlist.
    normalized = normalize_adapter_name(name)
    if normalized in SUPPORTED_ADAPTERS:
        return normalized
    return None


def adapter_label(adapter: str | None) -> str | None:
    """Return a user-facing label for a known adapter."""
    normalized = normalize_adapter_name(adapter)
    if normalized is None:
        return None
    return _ADAPTER_LABELS.get(normalized, normalized)
