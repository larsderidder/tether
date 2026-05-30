"""Bridge-friendly formatting for assistant, tool, and thinking output."""

from __future__ import annotations

from dataclasses import dataclass
import html
import re
from typing import Any

from tether.bridges.telegram.formatting import markdown_to_telegram_html

_RESERVED_MARKERS = {"tool", "thinking", "result", "error", "assistant"}
_DISCORD_LIMIT = 2000
_SLACK_LIMIT = 40000
_TELEGRAM_LIMIT = 4096


@dataclass(slots=True)
class OutputSegment:
    kind: str
    text: str
    label: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Serialize the segment for store event metadata."""

        data = {"kind": self.kind, "text": self.text}
        if self.label:
            data["label"] = self.label
        return data


def coerce_output_segments(value: object) -> list[OutputSegment]:
    """Convert serialized bridge segment metadata to output segments."""

    if not isinstance(value, list):
        return []

    segments: list[OutputSegment] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if not kind:
            continue
        segment = OutputSegment(
            kind=kind,
            text=str(item.get("text") or ""),
            label=str(item["label"]) if item.get("label") else None,
        )
        if _can_merge_segments(segments[-1] if segments else None, segment):
            segments[-1].text += segment.text
        else:
            segments.append(segment)
    return segments


def _can_merge_segments(
    previous: OutputSegment | None,
    current: OutputSegment,
) -> bool:
    """Return true when adjacent streamed segments are one logical block."""

    if previous is None:
        return False
    if previous.kind != current.kind or previous.label != current.label:
        return False
    return current.kind in {
        "assistant",
        "thinking",
        "tool_output",
        "result",
        "tool_result",
        "error",
        "tool_error",
    }


def parse_output_segments(text: str) -> list[OutputSegment]:
    """Split streamed bridge text into semantically distinct chunks."""
    if not text:
        return []

    segments: list[OutputSegment] = []
    current: OutputSegment | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        current.text = current.text.rstrip("\n")
        if current.text or current.kind == "tool_call":
            segments.append(current)
        current = None

    for raw_line in text.splitlines():
        tool_call = re.fullmatch(r"\[tool:\s*([^\]]+)\]\s*", raw_line)
        if tool_call:
            flush()
            current = OutputSegment("tool_call", "", tool_call.group(1).strip())
            flush()
            continue

        thinking = re.fullmatch(r"\[thinking\]\s*(.*)", raw_line)
        if thinking:
            flush()
            current = OutputSegment("thinking", thinking.group(1), "thinking")
            continue

        assistant = re.fullmatch(r"\[assistant\]\s*(.*)", raw_line)
        if assistant:
            flush()
            current = OutputSegment("assistant", assistant.group(1), "assistant")
            continue

        result = re.fullmatch(r"\[(result|error)\]\s*(.*)", raw_line)
        if result:
            flush()
            current = OutputSegment(result.group(1), result.group(2), result.group(1))
            continue

        tagged = re.fullmatch(r"\[([^\]]+)\]\s*(.*)", raw_line)
        if tagged:
            marker = tagged.group(1).strip()
            marker_lower = marker.lower()
            if marker_lower == "notify":
                flush()
                segments.append(OutputSegment("status", tagged.group(2), marker))
                current = None
                continue
            if marker_lower not in _RESERVED_MARKERS:
                flush()
                current = OutputSegment("tool_output", tagged.group(2), marker)
                continue
            flush()
            current = OutputSegment("info", raw_line, marker)
            continue

        if current is None:
            current = OutputSegment("assistant", raw_line)
        elif current.kind in {
            "assistant",
            "thinking",
            "tool_output",
            "result",
            "error",
            "info",
        }:
            if current.text:
                current.text += "\n"
            current.text += raw_line
        else:
            flush()
            current = OutputSegment("assistant", raw_line)

    flush()
    return segments


def _escape_code(text: str) -> str:
    return text.replace("```", "``\u200b`")


def _clean_thinking_markers(text: str) -> str:
    """Remove legacy inline thinking markers from token-streamed output."""

    cleaned = re.sub(r"\[thinking\]\s*", " ", text)
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def _normalize_plain_markdown(text: str) -> str:
    """Stabilize common Markdown syntax for chat renderers.

    This rewrites explicit list markers and Markdown tables while skipping
    fenced code blocks. It avoids guessing where newlines should go.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _markdown_tables_to_code_blocks(normalized)
    lines = normalized.split("\n")
    converted: list[str] = []
    in_code_block = False

    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            converted.append(line)
            continue

        if in_code_block:
            converted.append(line)
            continue

        unordered = re.match(r"^([-*+])\s+(.*)$", stripped)
        if unordered:
            converted.append(f"{indent}• {unordered.group(2)}")
            continue

        ordered = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if ordered:
            converted.append(f"{indent}{ordered.group(1)}) {ordered.group(2)}")
            continue

        converted.append(line)

    return "\n".join(converted)


def _markdown_tables_to_code_blocks(text: str) -> str:
    """Convert Markdown tables to aligned code blocks for chat renderers."""

    lines = text.split("\n")
    converted: list[str] = []
    index = 0
    in_code_block = False

    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            converted.append(line)
            index += 1
            continue

        if not in_code_block and _is_table_start(lines, index):
            table_lines = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and _is_table_row(lines[index]):
                table_lines.append(lines[index])
                index += 1
            rendered = _render_markdown_table(table_lines)
            if rendered:
                converted.append(rendered)
                continue
            converted.extend(table_lines)
            continue

        converted.append(line)
        index += 1

    return "\n".join(converted)


def _is_table_start(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and _is_table_row(lines[index])
        and _is_table_separator(lines[index + 1])
    )


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _is_table_separator(line: str) -> bool:
    if not _is_table_row(line):
        return False
    cells = _split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _render_markdown_table(table_lines: list[str]) -> str | None:
    if len(table_lines) < 3:
        return None
    rows = [_split_table_row(line) for line in table_lines]
    headers = rows[0]
    body = rows[2:]
    if not headers or not body:
        return None
    column_count = len(headers)
    if any(len(row) != column_count for row in body):
        return None
    clean_rows = [[_clean_table_cell(cell) for cell in row] for row in [headers, *body]]
    widths = [max(len(row[column]) for row in clean_rows) for column in range(column_count)]
    rendered_lines = []
    for row in clean_rows:
        rendered_lines.append("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip())
    return "```text\n" + "\n".join(rendered_lines) + "\n```"


def _clean_table_cell(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    return text.replace("`", "").strip()


def _chunk_plain(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _chunk_code_block(body: str, limit: int, language: str = "text") -> list[str]:
    fence_open = f"```{language}\n"
    fence_close = "\n```"
    available = max(1, limit - len(fence_open) - len(fence_close))
    escaped = _escape_code(body)
    chunks = []
    for i in range(0, len(escaped), available):
        part = escaped[i : i + available]
        chunks.append(f"{fence_open}{part}{fence_close}")
    return chunks or [f"{fence_open}{fence_close}"]


def render_markdown_segments(
    text: str,
    *,
    limit: int,
    bold: str = "**",
    segments: list[OutputSegment] | None = None,
) -> list[str]:
    """Render parsed or structured segments to Discord or Slack friendly markdown."""
    messages: list[str] = []
    for segment in segments or parse_output_segments(text):
        if segment.kind == "assistant":
            messages.extend(
                _chunk_plain(_normalize_plain_markdown(segment.text), limit)
            )
        elif segment.kind == "thinking":
            body = _clean_thinking_markers(segment.text).strip() or "Thinking"
            quote = "\n".join(f"> {line}" for line in body.splitlines())
            messages.extend(_chunk_plain(f"💭 {bold}Thinking{bold}\n{quote}", limit))
        elif segment.kind == "tool_call":
            messages.extend(
                _chunk_plain(
                    f"🔧 {bold}Tool call{bold} `{segment.label or 'tool'}`", limit
                )
            )
        elif segment.kind == "tool_output":
            header = f"📥 {bold}Tool output{bold} `{segment.label or 'tool'}`\n"
            body_chunks = _chunk_code_block(segment.text or " ", limit - len(header))
            messages.extend(header + chunk for chunk in body_chunks)
        elif segment.kind in {"result", "tool_result"}:
            label = (
                f" `{segment.label}`"
                if segment.label and segment.label != segment.kind
                else ""
            )
            header = f"📥 {bold}Tool result{bold}{label}\n"
            body_chunks = _chunk_code_block(segment.text or " ", limit - len(header))
            messages.extend(header + chunk for chunk in body_chunks)
        elif segment.kind in {"error", "tool_error"}:
            label = (
                f" `{segment.label}`"
                if segment.label and segment.label != segment.kind
                else ""
            )
            header = f"⚠️ {bold}Tool error{bold}{label}\n"
            body_chunks = _chunk_code_block(segment.text or " ", limit - len(header))
            messages.extend(header + chunk for chunk in body_chunks)
        elif segment.kind == "status":
            messages.extend(_chunk_plain(f"ℹ️ {segment.text}", limit))
        else:
            messages.extend(_chunk_plain(f"ℹ️ {segment.text}", limit))
    return [message for message in messages if message.strip()]


def _segments_from_metadata(metadata: dict[str, Any] | None) -> list[OutputSegment]:
    """Extract structured bridge segments from output metadata."""

    return coerce_output_segments((metadata or {}).get("bridge_segments"))


def render_discord_messages(
    text: str, metadata: dict[str, Any] | None = None
) -> list[str]:
    """Render output segments for Discord."""

    return render_markdown_segments(
        text,
        limit=_DISCORD_LIMIT,
        segments=_segments_from_metadata(metadata),
    )


def render_slack_messages(
    text: str, metadata: dict[str, Any] | None = None
) -> list[str]:
    """Render output segments for Slack."""

    return render_markdown_segments(
        text,
        limit=_SLACK_LIMIT,
        bold="*",
        segments=_segments_from_metadata(metadata),
    )


def render_telegram_messages(
    text: str, metadata: dict[str, Any] | None = None
) -> list[str]:
    """Render parsed or structured segments to Telegram HTML messages."""
    messages: list[str] = []
    for segment in _segments_from_metadata(metadata) or parse_output_segments(text):
        if segment.kind == "assistant":
            rendered = markdown_to_telegram_html(
                _normalize_plain_markdown(segment.text)
            )
            messages.extend(_chunk_plain(rendered, _TELEGRAM_LIMIT))
        elif segment.kind == "thinking":
            body = html.escape(_clean_thinking_markers(segment.text).strip() or "Thinking")
            rendered = f"💭 <b>Thinking</b>\n<i>{body}</i>"
            messages.extend(_chunk_plain(rendered, _TELEGRAM_LIMIT))
        elif segment.kind == "tool_call":
            label = html.escape(segment.label or "tool")
            messages.extend(
                _chunk_plain(
                    f"🔧 <b>Tool call</b> <code>{label}</code>",
                    _TELEGRAM_LIMIT,
                )
            )
        elif segment.kind == "tool_output":
            label = html.escape(segment.label or "tool")
            header = f"📥 <b>Tool output</b> <code>{label}</code>\n"
            body_chunks = _chunk_plain(
                html.escape(segment.text or " "),
                _TELEGRAM_LIMIT - len(header) - 11,
            )
            messages.extend(header + f"<pre>{chunk}</pre>" for chunk in body_chunks)
        elif segment.kind in {"result", "tool_result"}:
            label = (
                f" <code>{html.escape(segment.label)}</code>"
                if segment.label and segment.label != segment.kind
                else ""
            )
            header = f"📥 <b>Tool result</b>{label}\n"
            body_chunks = _chunk_plain(
                html.escape(segment.text or " "),
                _TELEGRAM_LIMIT - len(header) - 11,
            )
            messages.extend(header + f"<pre>{chunk}</pre>" for chunk in body_chunks)
        elif segment.kind in {"error", "tool_error"}:
            label = (
                f" <code>{html.escape(segment.label)}</code>"
                if segment.label and segment.label != segment.kind
                else ""
            )
            header = f"⚠️ <b>Tool error</b>{label}\n"
            body_chunks = _chunk_plain(
                html.escape(segment.text or " "),
                _TELEGRAM_LIMIT - len(header) - 11,
            )
            messages.extend(header + f"<pre>{chunk}</pre>" for chunk in body_chunks)
        elif segment.kind == "status":
            rendered = f"ℹ️ {html.escape(segment.text)}"
            messages.extend(_chunk_plain(rendered, _TELEGRAM_LIMIT))
        else:
            rendered = f"ℹ️ {html.escape(segment.text)}"
            messages.extend(_chunk_plain(rendered, _TELEGRAM_LIMIT))
    return [message for message in messages if message.strip()]


__all__ = [
    "OutputSegment",
    "coerce_output_segments",
    "parse_output_segments",
    "render_discord_messages",
    "render_slack_messages",
    "render_telegram_messages",
]
