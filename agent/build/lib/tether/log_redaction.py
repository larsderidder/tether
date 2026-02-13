"""Log redaction helpers.

We redact tokens and other credentials from structured logs before they hit stdout.

This is deterministic key-based redaction plus a couple of string rules (not PII detection).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, cast

from tether.settings import settings


_SENSITIVE_KEYWORDS: list[str] = [
    # Common auth
    "authorization",
    "bearer",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "client_secret",
    # Project-specific / integrations
    "tether_agent_token",
    "tether_codex_sidecar_token",
    "anthropic_api_key",
    "slack_bot_token",
    "slack_app_token",
    "telegram_bot_token",
    "discord_bot_token",
]


def _fallback_redact(value: Any, *, string_rules: list[re.Pattern[str]], replacement: str) -> Any:
    """Best-effort local redaction when payload-redactor isn't importable."""

    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:
            return replacement

    if isinstance(value, str):
        redacted = value
        for pat in string_rules:
            redacted = pat.sub(replacement, redacted)
        return redacted

    if isinstance(value, (list, tuple)):
        return [ _fallback_redact(v, string_rules=string_rules, replacement=replacement) for v in value ]

    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            key_str = str(k).lower()
            if any(word in key_str for word in _SENSITIVE_KEYWORDS):
                out[k] = replacement
                continue
            out[k] = _fallback_redact(v, string_rules=string_rules, replacement=replacement)
        return out

    # Unknown / custom objects: redact their string representation.
    return _fallback_redact(str(value), string_rules=string_rules, replacement=replacement)


def _collect_exact_secrets() -> list[str]:
    """Collect known secret values from env/settings.

These are used as string redaction rules so that secrets are still redacted even
if they accidentally get logged under an unexpected key.
"""

    candidates = [
        settings.token(),
        settings.codex_sidecar_token(),
        settings.anthropic_api_key(),
        settings.telegram_bot_token(),
        settings.slack_bot_token(),
        settings.slack_app_token(),
        settings.discord_bot_token(),
    ]
    out: list[str] = []
    for value in candidates:
        value = (value or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def make_log_redactor(*, replacement: str = "[REDACTED]") -> Callable[[Any, str, dict], dict]:
    """Create a structlog processor that redacts secrets from event_dict."""

    # Redact bearer tokens even when the key is not a known sensitive keyword.
    string_rules: list[str] = [r"Bearer\s+\S+"]

    # Redact exact secret values if they appear anywhere in a string payload.
    for secret in _collect_exact_secrets():
        string_rules.append(re.escape(secret))

    compiled_rules = [re.compile(rule) for rule in string_rules]

    try:
        from payload_redactor import Policy, redact  # type: ignore

        policy = Policy(
            sensitive_keywords=_SENSITIVE_KEYWORDS,
            string_rules=string_rules,
        )

        try:
            from payload_redactor.structlog import (  # type: ignore
                make_structlog_redactor as _make_structlog_redactor,
            )

            import inspect

            sig = inspect.signature(_make_structlog_redactor)
            if "policy" in sig.parameters:
                kwargs: dict[str, Any] = {"policy": policy}
                if "replacement" in sig.parameters:
                    kwargs["replacement"] = replacement
                return _make_structlog_redactor(**kwargs)
        except Exception:
            pass

        def _processor(logger: Any, name: str, event_dict: dict) -> dict:
            # `redact` preserves input types; structlog requires we return a dict.
            return cast(dict, redact(event_dict, policy=policy, replacement=replacement))

        return _processor
    except Exception:
        # Fall back to local redaction instead of failing open.
        def _processor(logger: Any, name: str, event_dict: dict) -> dict:
            return cast(
                dict,
                _fallback_redact(
                    event_dict, string_rules=compiled_rules, replacement=replacement
                ),
            )

        return _processor
