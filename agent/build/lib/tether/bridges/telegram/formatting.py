"""Compatibility shim: re-exports from agent_tether.telegram.formatting."""
# ruff: noqa: F401
from agent_tether.telegram.formatting import *  # noqa: F403
from agent_tether.telegram.formatting import (
    _markdown_table_to_pre,
    chunk_message,
    escape_markdown,
    markdown_to_telegram_html,
    strip_tool_markers,
)
