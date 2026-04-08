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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Compatibility shim for older agent-tether versions.
        if not hasattr(self, "_external_view_by_user"):
            self._external_view_by_user: dict[str, list[dict]] = {}

    async def _cmd_list(self, event: dict, args: str) -> None:
        await super()._cmd_list(event, args)
        user_id = str(event.get("user") or "")
        view = getattr(self, "_external_view", None)
        if user_id and isinstance(view, list):
            self._external_view_by_user[user_id] = list(view)

    async def _cmd_attach(self, event: dict, args: str) -> None:
        user_id = str(event.get("user") or "")
        user_view = self._external_view_by_user.get(user_id) if user_id else None

        if isinstance(user_view, list) and hasattr(self, "_external_view"):
            original_view = list(getattr(self, "_external_view", []))
            try:
                self._external_view = list(user_view)
                await super()._cmd_attach(event, args)
            finally:
                self._external_view = original_view
            return

        await super()._cmd_attach(event, args)

    async def on_output(self, session_id: str, text: str, metadata: dict | None = None) -> None:
        normalized = _normalize_slack_output_text(text)
        await super().on_output(session_id, normalized, metadata)
