"""Shared helpers for reaction-driven session creation shortcuts."""

from __future__ import annotations

from dataclasses import dataclass

_CHECKMARK_CANONICAL = "white_check_mark"
_CHECKMARK_ALIASES = {
    "✅": _CHECKMARK_CANONICAL,
    "checkmark": _CHECKMARK_CANONICAL,
    "heavy_check_mark": _CHECKMARK_CANONICAL,
    "white_check_mark": _CHECKMARK_CANONICAL,
    "white-heavy-check-mark": _CHECKMARK_CANONICAL,
}


@dataclass(frozen=True)
class ReactionShortcutRequest:
    """Parsed ``!new`` reaction shortcut payload."""

    args: str | None
    prompt: str


class ReactionShortcutError(ValueError):
    """User-facing validation error for the reaction shortcut."""


def canonical_reaction_name(raw: str) -> str:
    """Normalize a reaction token for Slack and Discord matching."""
    token = (raw or "").strip()
    if not token:
        return ""
    token = token.strip(":")
    return _CHECKMARK_ALIASES.get(token, token.casefold())


def reaction_matches(configured_reaction: str, actual_reaction: str) -> bool:
    """Return True when the configured shortcut reaction matches the incoming one."""
    return canonical_reaction_name(configured_reaction) == canonical_reaction_name(
        actual_reaction
    )


def parse_reaction_shortcut_message(
    text: str, *, allow_plain_message: bool = False
) -> ReactionShortcutRequest | None:
    """Parse a top-level control-channel message for the reaction shortcut.

    Returns ``None`` when the message is not a shortcut candidate. Raises
    ``ReactionShortcutError`` when the message opts in via ``!new`` but does not
    provide the required prompt body. When ``allow_plain_message`` is enabled,
    a non-command message is treated as the prompt and uses the bridge default
    adapter plus current working directory.
    """
    normalized = (text or "").strip()
    if not normalized:
        return None

    lines = normalized.splitlines()
    header = lines[0].strip()
    if not header.lower().startswith("!new"):
        if allow_plain_message and not header.startswith("!"):
            return ReactionShortcutRequest(args=None, prompt=normalized)
        return None

    args = header[4:].strip()
    if not args:
        raise ReactionShortcutError(
            "First line must use `!new <agent> <directory>` or `!new <directory>`."
        )

    prompt = "\n".join(line.rstrip() for line in lines[1:]).strip()
    if not prompt:
        raise ReactionShortcutError(
            "Add a prompt below the `!new ...` line before reacting."
        )

    return ReactionShortcutRequest(args=args, prompt=prompt)
