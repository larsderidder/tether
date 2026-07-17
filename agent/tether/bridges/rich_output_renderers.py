"""Platform-specific rich-output renderers for bridges."""

from __future__ import annotations

import html
from typing import Any

from tether.bridges.rich_output_markdown import (
    _chunk_plain,
    _clean_thinking_markers,
    _markdown_to_telegram_html,
    _normalize_plain_markdown,
    _normalize_telegram_markdown,
)
from tether.bridges.rich_output_models import OutputSegment, RenderedBridgeMessage
from tether.bridges.rich_output_segments import parse_output_segments
from tether.bridges.rich_output_tools import (
    _is_tool_activity_bundle,
    _render_markdown_tool_activity_bundle,
    _render_telegram_tool_activity_bundle,
    _render_telegram_tool_messages,
    _render_tool_segment,
    _segments_from_metadata,
    _tool_segment_html_title,
    _truncate_tool_body,
)

_DISCORD_LIMIT = 2000
_SLACK_LIMIT = 40000
_TELEGRAM_LIMIT = 4096
_DISCORD_TOOL_OUTPUT_INLINE_LINES = 6
_DISCORD_TOOL_OUTPUT_INLINE_CHARS = 800
_TOOL_EXPAND_REACTION = "📄"


def render_markdown_segments(
    text: str,
    *,
    limit: int,
    bold: str = "**",
    segments: list[OutputSegment] | None = None,
) -> list[str]:
    """Render parsed or structured segments to Discord or Slack friendly markdown."""

    return [
        message.text
        for message in render_markdown_messages(
            text,
            limit=limit,
            bold=bold,
            segments=segments,
        )
    ]


def render_markdown_messages(
    text: str,
    *,
    limit: int,
    bold: str = "**",
    segments: list[OutputSegment] | None = None,
    truncate_tool_outputs: bool = False,
) -> list[RenderedBridgeMessage]:
    """Render output segments, optionally attaching full tool-output expansions."""

    messages: list[RenderedBridgeMessage] = []
    for segment in segments or parse_output_segments(text):
        if segment.kind == "assistant":
            messages.extend(
                RenderedBridgeMessage(chunk)
                for chunk in _chunk_plain(
                    _normalize_plain_markdown(segment.text), limit
                )
            )
        elif segment.kind == "thinking":
            body = _clean_thinking_markers(segment.text).strip() or "Thinking"
            quote = "\n".join(f"> {line}" for line in body.splitlines())
            messages.extend(
                RenderedBridgeMessage(chunk)
                for chunk in _chunk_plain(f"💭 {bold}Thinking{bold}\n{quote}", limit)
            )
        elif segment.kind == "tool_call":
            messages.extend(
                RenderedBridgeMessage(chunk)
                for chunk in _chunk_plain(
                    f"🔧 {bold}Tool call{bold} `{segment.label or 'tool'}`", limit
                )
            )
        elif segment.kind == "tool_output":
            messages.extend(
                _render_tool_segment(
                    segment,
                    title="Tool output",
                    limit=limit,
                    bold=bold,
                    truncate=truncate_tool_outputs,
                )
            )
        elif segment.kind in {"result", "tool_result"}:
            label = (
                f" `{segment.label}`"
                if segment.label and segment.label != segment.kind
                else ""
            )
            messages.extend(
                _render_tool_segment(
                    segment,
                    title="Tool result",
                    label=label,
                    limit=limit,
                    bold=bold,
                    truncate=truncate_tool_outputs,
                )
            )
        elif segment.kind in {"error", "tool_error"}:
            label = (
                f" `{segment.label}`"
                if segment.label and segment.label != segment.kind
                else ""
            )
            messages.extend(
                _render_tool_segment(
                    segment,
                    title="Tool error",
                    label=label,
                    limit=limit,
                    bold=bold,
                    truncate=truncate_tool_outputs,
                    icon="⚠️",
                )
            )
        elif segment.kind == "status":
            messages.extend(
                RenderedBridgeMessage(chunk)
                for chunk in _chunk_plain(f"ℹ️ {segment.text}", limit)
            )
        else:
            messages.extend(
                RenderedBridgeMessage(chunk)
                for chunk in _chunk_plain(f"ℹ️ {segment.text}", limit)
            )
    return [message for message in messages if message.text.strip()]


def render_discord_message_objects(
    text: str, metadata: dict[str, Any] | None = None
) -> list[RenderedBridgeMessage]:
    """Render output segments for Discord with optional expansion payloads."""

    segments = _segments_from_metadata(metadata)
    if _is_tool_activity_bundle(metadata, segments):
        return _render_markdown_tool_activity_bundle(
            segments,
            limit=_DISCORD_LIMIT,
            bold="**",
        )

    return render_markdown_messages(
        text,
        limit=_DISCORD_LIMIT,
        segments=segments,
        truncate_tool_outputs=True,
    )


def render_discord_messages(
    text: str, metadata: dict[str, Any] | None = None
) -> list[str]:
    """Render output segments for Discord."""

    return [
        message.text
        for message in render_discord_message_objects(text, metadata=metadata)
    ]


def render_slack_messages(
    text: str, metadata: dict[str, Any] | None = None
) -> list[str]:
    """Render output segments for Slack."""

    segments = _segments_from_metadata(metadata)
    if _is_tool_activity_bundle(metadata, segments):
        return [
            message.text
            for message in _render_markdown_tool_activity_bundle(
                segments,
                limit=_SLACK_LIMIT,
                bold="*",
            )
        ]

    return [
        message.text
        for message in render_markdown_messages(
            text,
            limit=_SLACK_LIMIT,
            bold="*",
            segments=segments,
            truncate_tool_outputs=True,
        )
    ]


def _render_telegram_stream_batch(
    text: str,
    segments: list[OutputSegment],
) -> list[str]:
    """Render mixed buffered activity as one readable Telegram update."""
    if not segments:
        rendered = _markdown_to_telegram_html(_normalize_telegram_markdown(text))
        return _chunk_plain(rendered, _TELEGRAM_LIMIT)

    parts: list[str] = []
    prose_parts: list[str] = []

    def flush_prose() -> None:
        prose = "".join(prose_parts).strip()
        prose_parts.clear()
        if prose:
            parts.append(
                _markdown_to_telegram_html(_normalize_telegram_markdown(prose))
            )

    for segment in segments:
        if segment.kind == "assistant":
            prose_parts.append(segment.text)
            continue

        flush_prose()
        if segment.kind == "thinking":
            body = html.escape(
                _clean_thinking_markers(segment.text).strip() or "Thinking"
            )
            parts.append(f"💭 <i>{body}</i>")
        elif segment.kind == "tool_call":
            label = html.escape(segment.label or "tool")
            parts.append(f"🔧 <b>Tool call</b> <code>{label}</code>")
        elif segment.kind in {
            "tool_output",
            "result",
            "tool_result",
            "error",
            "tool_error",
        }:
            icon, title = _tool_segment_html_title(segment)
            label = html.escape(segment.label or "tool")
            preview, footer = _truncate_tool_body(segment.text or " ")
            parts.append(
                f"{icon} <b>{title}</b> <code>{label}</code>\n"
                f"<pre>{html.escape(preview or ' ')}</pre>{html.escape(footer)}"
            )
        elif segment.kind == "status":
            parts.append(f"ℹ️ {html.escape(segment.text)}")
        else:
            parts.append(html.escape(segment.text))

    flush_prose()
    rendered = "\n\n".join(part for part in parts if part.strip())
    return _chunk_plain(rendered, _TELEGRAM_LIMIT)


def render_telegram_messages(
    text: str, metadata: dict[str, Any] | None = None
) -> list[str]:
    """Render parsed or structured segments to Telegram HTML messages."""

    metadata_segments = _segments_from_metadata(metadata)
    if (metadata or {}).get("stream_batch"):
        return _render_telegram_stream_batch(text, metadata_segments)
    if _is_tool_activity_bundle(metadata, metadata_segments):
        return _render_telegram_tool_activity_bundle(metadata_segments)

    messages: list[str] = []
    for segment in metadata_segments or parse_output_segments(text):
        if segment.kind == "assistant":
            rendered = _markdown_to_telegram_html(
                _normalize_telegram_markdown(segment.text)
            )
            messages.extend(_chunk_plain(rendered, _TELEGRAM_LIMIT))
        elif segment.kind == "thinking":
            body = html.escape(
                _clean_thinking_markers(segment.text).strip() or "Thinking"
            )
            rendered = f"💭 <b>Thinking</b>\n<i>{body}</i>"
            messages.extend(_chunk_plain(rendered, _TELEGRAM_LIMIT))
        elif segment.kind == "tool_call":
            label = html.escape(segment.label or "tool")
            messages.extend(
                _chunk_plain(
                    f"🔧 <b>Tool call</b> <code>{label}</code>", _TELEGRAM_LIMIT
                )
            )
        elif segment.kind == "tool_output":
            messages.extend(
                _render_telegram_tool_messages(segment, title="Tool output")
            )
        elif segment.kind in {"result", "tool_result"}:
            label = (
                f" <code>{html.escape(segment.label)}</code>"
                if segment.label and segment.label != segment.kind
                else ""
            )
            messages.extend(
                _render_telegram_tool_messages(
                    segment,
                    title="Tool result",
                    label=label,
                )
            )
        elif segment.kind in {"error", "tool_error"}:
            label = (
                f" <code>{html.escape(segment.label)}</code>"
                if segment.label and segment.label != segment.kind
                else ""
            )
            messages.extend(
                _render_telegram_tool_messages(
                    segment,
                    title="Tool error",
                    label=label,
                    icon="⚠️",
                )
            )
        elif segment.kind == "automation_message":
            rendered = _markdown_to_telegram_html(
                _normalize_telegram_markdown(segment.text)
            )
            messages.extend(_chunk_plain(rendered, _TELEGRAM_LIMIT))
        elif segment.kind == "status":
            rendered = f"ℹ️ {html.escape(segment.text)}"
            messages.extend(_chunk_plain(rendered, _TELEGRAM_LIMIT))
        else:
            rendered = html.escape(segment.text)
            messages.extend(_chunk_plain(rendered, _TELEGRAM_LIMIT))
    return [message for message in messages if message.strip()]
