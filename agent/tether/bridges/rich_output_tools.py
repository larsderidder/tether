"""Tool activity rendering helpers for bridge rich output."""

from __future__ import annotations

import html
import re
from typing import Any

from tether.bridges.rich_output_markdown import _chunk_code_block, _chunk_plain
from tether.bridges.rich_output_models import OutputSegment, RenderedBridgeMessage
from tether.bridges.rich_output_segments import coerce_output_segments
from tether.settings import settings

_TELEGRAM_LIMIT = 4096
_TOOL_ACTIVITY_KINDS = {
    "tool_call",
    "tool_output",
    "tool_result",
    "tool_error",
    "result",
    "error",
}


def _truncate_tool_body(body: str) -> tuple[str, str]:
    """Return a shared bridge tool preview and footer when output is too large."""

    lines = body.splitlines()
    line_limit = settings.bridge_tool_output_inline_lines()
    char_limit = settings.bridge_tool_output_inline_chars()
    line_truncated = len(lines) > line_limit
    preview_lines = lines[:line_limit] if line_truncated else lines
    preview = "\n".join(preview_lines) if lines else body
    char_truncated = len(preview) > char_limit
    if char_truncated:
        preview = preview[:char_limit].rstrip()

    notes = []
    if line_truncated:
        notes.append(f"{len(lines) - line_limit:,} lines")
    if char_truncated:
        notes.append("chars")
    if not notes:
        return body, ""
    return preview.rstrip(), f"\n… truncated {' and '.join(notes)}."


def _render_tool_segment(
    segment: OutputSegment,
    *,
    title: str,
    limit: int,
    bold: str,
    truncate: bool,
    label: str | None = None,
    icon: str = "📥",
) -> list[RenderedBridgeMessage]:
    """Render one Markdown tool segment."""

    label_text = label if label is not None else f" `{segment.label or 'tool'}`"
    header = f"{icon} {bold}{title}{bold}{label_text}\n"
    body = segment.text or " "
    if truncate:
        preview, footer = _truncate_tool_body(body)
        if footer:
            chunk = _chunk_code_block(
                preview or " ", limit - len(header) - len(footer)
            )[0]
            return [
                RenderedBridgeMessage(
                    text=header + chunk + footer,
                    expansion_text=body,
                    expansion_filename=f"{_safe_expansion_name(segment.label or title)}.txt",
                )
            ]

    body_chunks = _chunk_code_block(body, limit - len(header))
    return [RenderedBridgeMessage(header + chunk) for chunk in body_chunks]


def _safe_expansion_name(value: str) -> str:
    """Return a safe filename stem for bridge expansion attachments."""

    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip("-._")
    return safe[:80] or "tool-output"


def _segments_from_metadata(metadata: dict[str, Any] | None) -> list[OutputSegment]:
    """Extract structured bridge segments from output metadata."""

    return coerce_output_segments((metadata or {}).get("bridge_segments"))


def _is_tool_activity_bundle(
    metadata: dict[str, Any] | None,
    segments: list[OutputSegment],
) -> bool:
    """Return true when metadata represents one buffered tool activity bundle."""

    return bool(
        settings.bridge_tool_activity_combine_messages()
        and (metadata or {}).get("tool_activity")
        and segments
        and all(segment.kind in _TOOL_ACTIVITY_KINDS for segment in segments)
    )


def _tool_segment_heading(segment: OutputSegment, *, bold: str) -> tuple[str, str]:
    """Return icon and markdown title for a tool segment."""

    if segment.kind == "tool_call":
        return "🔧", f"{bold}Tool call{bold}"
    if segment.kind in {"error", "tool_error"}:
        return "⚠️", f"{bold}Tool error{bold}"
    if segment.kind in {"result", "tool_result"}:
        return "📥", f"{bold}Tool result{bold}"
    return "📥", f"{bold}Tool output{bold}"


def _render_markdown_tool_activity_bundle(
    segments: list[OutputSegment],
    *,
    limit: int,
    bold: str,
) -> list[RenderedBridgeMessage]:
    """Render multiple tool segments as one Discord or Slack message."""

    parts = [f"🔧 {bold}Tool activity{bold}"]
    expansion_parts: list[str] = []
    for segment in segments:
        icon, title = _tool_segment_heading(segment, bold=bold)
        label = segment.label or "tool"
        if segment.kind == "tool_call":
            parts.append(f"{icon} {title} `{label}`")
            continue

        body = segment.text or " "
        preview, footer = _truncate_tool_body(body)
        parts.append(
            f"{icon} {title} `{label}`\n```text\n{preview or ' '}\n```{footer}"
        )
        if footer:
            expansion_parts.append(f"{title.replace(bold, '')} {label}\n{body}")

    full_text = "\n\n".join(parts)
    expansion_text = "\n\n".join(expansion_parts) or None
    if len(full_text) > limit:
        suffix = "\n… truncated tool activity."
        expansion_text = full_text
        full_text = full_text[: max(0, limit - len(suffix))].rstrip() + suffix

    return [
        RenderedBridgeMessage(
            text=full_text,
            expansion_text=expansion_text,
            expansion_filename="tool-activity.txt",
        )
    ]


def _tool_segment_html_title(segment: OutputSegment) -> tuple[str, str]:
    """Return icon and HTML title for a Telegram tool segment."""

    if segment.kind == "tool_call":
        return "🔧", "Tool call"
    if segment.kind in {"error", "tool_error"}:
        return "⚠️", "Tool error"
    if segment.kind in {"result", "tool_result"}:
        return "📥", "Tool result"
    return "📥", "Tool output"


def _render_telegram_tool_activity_bundle(
    segments: list[OutputSegment],
) -> list[str]:
    """Render multiple tool segments as compact Telegram HTML."""

    parts = ["🔧 <b>Tool activity</b>"]
    for segment in segments:
        icon, title = _tool_segment_html_title(segment)
        label = html.escape(segment.label or "tool")
        if segment.kind == "tool_call":
            parts.append(f"{icon} <b>{title}</b> <code>{label}</code>")
            continue

        preview, footer = _truncate_tool_body(segment.text or " ")
        parts.append(
            f"{icon} <b>{title}</b> <code>{label}</code>\n"
            f"<pre>{html.escape(preview or ' ')}</pre>{html.escape(footer)}"
        )

    return _chunk_plain("\n\n".join(parts), _TELEGRAM_LIMIT)


def _render_telegram_tool_messages(
    segment: OutputSegment,
    *,
    title: str,
    icon: str = "📥",
    label: str | None = None,
) -> list[str]:
    """Render one tool segment as compact Telegram HTML."""

    label_text = (
        label
        if label is not None
        else f" <code>{html.escape(segment.label or 'tool')}</code>"
    )
    header = f"{icon} <b>{title}</b>{label_text}\n"
    body = segment.text or " "
    preview, footer = _truncate_tool_body(body)
    body_chunks = _chunk_plain(
        html.escape(preview or " "),
        _TELEGRAM_LIMIT - len(header) - len(footer) - 11,
    )
    rendered = [header + f"<pre>{chunk}</pre>" for chunk in body_chunks]
    if footer and rendered:
        rendered[-1] += html.escape(footer)
    return rendered
