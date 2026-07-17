"""Compatibility facade for bridge-friendly rich-output formatting."""

from __future__ import annotations

# ruff: noqa: F401

from dataclasses import dataclass as dataclass
import html as html
import re as re
from typing import Any as Any

from tether.bridges.telegram.formatting import (
    markdown_to_telegram_html as markdown_to_telegram_html,
)
from tether.settings import settings as settings
from tether.bridges.rich_output_markdown import (
    _FENCED_CODE_RE,
    _chunk_code_block,
    _chunk_plain,
    _clean_table_cell,
    _clean_thinking_markers,
    _convert_markdown_tables,
    _escape_code,
    _is_table_row,
    _is_table_separator,
    _is_table_start,
    _markdown_tables_to_code_blocks,
    _markdown_tables_to_telegram_cards,
    _markdown_to_telegram_html,
    _normalize_list_markers,
    _normalize_plain_markdown,
    _normalize_telegram_markdown,
    _render_markdown_table,
    _render_markdown_table_as_cards,
    _split_table_row,
)
from tether.bridges.rich_output_models import OutputSegment, RenderedBridgeMessage
from tether.bridges.rich_output_renderers import (
    _DISCORD_LIMIT,
    _DISCORD_TOOL_OUTPUT_INLINE_CHARS,
    _DISCORD_TOOL_OUTPUT_INLINE_LINES,
    _SLACK_LIMIT,
    _TELEGRAM_LIMIT,
    _TOOL_EXPAND_REACTION,
    render_discord_message_objects,
    render_discord_messages,
    render_markdown_messages,
    render_markdown_segments,
    render_slack_messages,
    render_telegram_messages,
)
from tether.bridges.rich_output_segments import (
    _RESERVED_MARKERS,
    _can_merge_segments,
    coerce_output_segments,
    parse_output_segments,
)
from tether.bridges.rich_output_tools import (
    _TOOL_ACTIVITY_KINDS,
    _is_tool_activity_bundle,
    _render_markdown_tool_activity_bundle,
    _render_telegram_tool_activity_bundle,
    _render_telegram_tool_messages,
    _render_tool_segment,
    _safe_expansion_name,
    _segments_from_metadata,
    _tool_segment_heading,
    _tool_segment_html_title,
    _truncate_tool_body,
)

__all__ = [
    "RenderedBridgeMessage",
    "OutputSegment",
    "coerce_output_segments",
    "parse_output_segments",
    "render_discord_message_objects",
    "render_discord_messages",
    "render_slack_messages",
    "render_telegram_messages",
]
