"""Slack bridge wrapper with Tether-specific output normalization."""

from __future__ import annotations

from agent_tether.slack.bot import SlackBridge as _BaseSlackBridge

__all__ = ["SlackBridge"]


def _normalize_slack_output_text(text: str) -> str:
    """Normalize markdown list markers for reliable Slack rendering."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    marker_count = sum(
        1
        for line in lines
        if line.lstrip().startswith("- ") or line.lstrip().startswith("* ")
    )
    if marker_count < 2:
        return normalized

    converted: list[str] = []
    in_code_block = False
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            converted.append(line)
            continue

        if not in_code_block and (stripped.startswith("- ") or stripped.startswith("* ")):
            converted.append(f"{indent}• {stripped[2:]}")
        else:
            converted.append(line)

    return "\n".join(converted)


class SlackBridge(_BaseSlackBridge):
    """Tether Slack bridge wrapper."""

    async def on_output(self, session_id: str, text: str, metadata: dict | None = None) -> None:
        normalized = _normalize_slack_output_text(text)
        await super().on_output(session_id, normalized, metadata)
