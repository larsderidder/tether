"""Markdown normalization and chunking helpers for bridge output."""

from __future__ import annotations

from collections.abc import Callable
import html
import re

from tether.bridges.telegram.formatting import markdown_to_telegram_html

_FENCED_CODE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
TableRenderer = Callable[[list[str]], str | None]


def _escape_code(text: str) -> str:
    """Escape Markdown code fences inside generated code blocks."""

    return text.replace("```", "``\u200b`")


def _clean_thinking_markers(text: str) -> str:
    """Remove legacy inline thinking markers from token-streamed output."""

    cleaned = re.sub(r"\[thinking\]\s*", " ", text)
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def _markdown_to_telegram_html(text: str) -> str:
    """Convert Markdown to Telegram HTML while preserving fenced code bodies."""

    code_blocks: list[str] = []

    def protect_code(match: re.Match) -> str:
        body = html.escape(match.group(1).rstrip())
        code_blocks.append(f"<pre>{body}</pre>")
        return f"\ue000TETHER_CODE_BLOCK_{len(code_blocks) - 1}\ue000"

    protected = _FENCED_CODE_RE.sub(protect_code, text)
    rendered = markdown_to_telegram_html(protected)
    for index, code_block in enumerate(code_blocks):
        rendered = rendered.replace(
            f"\ue000TETHER_CODE_BLOCK_{index}\ue000", code_block
        )
    return rendered


def _normalize_plain_markdown(text: str) -> str:
    """Stabilize common Markdown syntax for chat renderers."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _markdown_tables_to_code_blocks(normalized)
    return _normalize_list_markers(normalized)


def _normalize_telegram_markdown(text: str) -> str:
    """Stabilize Markdown for Telegram without fixed-width tables."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _markdown_tables_to_telegram_cards(normalized)
    return _normalize_list_markers(normalized)


def _normalize_list_markers(text: str) -> str:
    """Convert Markdown list markers to platform-safe plain text markers."""

    lines = text.split("\n")
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

    return _convert_markdown_tables(text, _render_markdown_table)


def _markdown_tables_to_telegram_cards(text: str) -> str:
    """Convert Markdown tables to stacked cards for Telegram mobile."""

    return _convert_markdown_tables(text, _render_markdown_table_as_cards)


def _convert_markdown_tables(text: str, renderer: TableRenderer) -> str:
    """Convert Markdown tables with the supplied renderer outside code blocks."""

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
            rendered = renderer(table_lines)
            if rendered:
                converted.append(rendered)
                continue
            converted.extend(table_lines)
            continue

        converted.append(line)
        index += 1

    return "\n".join(converted)


def _is_table_start(lines: list[str], index: int) -> bool:
    """Return true when a line starts a Markdown table."""

    return (
        index + 1 < len(lines)
        and _is_table_row(lines[index])
        and _is_table_separator(lines[index + 1])
    )


def _is_table_row(line: str) -> bool:
    """Return true when a line looks like a Markdown table row."""

    stripped = line.strip()
    return (
        stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2
    )


def _is_table_separator(line: str) -> bool:
    """Return true when a line is a Markdown table separator."""

    if not _is_table_row(line):
        return False
    cells = _split_table_row(line)
    return bool(cells) and all(
        re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells
    )


def _split_table_row(line: str) -> list[str]:
    """Split one Markdown table row into cells."""

    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _render_markdown_table(table_lines: list[str]) -> str | None:
    """Render a Markdown table as an aligned code block."""

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
    widths = [
        max(len(row[column]) for row in clean_rows) for column in range(column_count)
    ]
    rendered_lines = []
    for row in clean_rows:
        rendered_lines.append(
            "  ".join(
                cell.ljust(widths[index]) for index, cell in enumerate(row)
            ).rstrip()
        )
    return "```text\n" + "\n".join(rendered_lines) + "\n```"


def _render_markdown_table_as_cards(table_lines: list[str]) -> str | None:
    """Render a Markdown table as mobile-friendly cards."""

    if len(table_lines) < 3:
        return None
    rows = [_split_table_row(line) for line in table_lines]
    headers = [
        _clean_table_cell(cell) or f"Column {index}"
        for index, cell in enumerate(rows[0], start=1)
    ]
    body = rows[2:]
    if not headers or not body:
        return None
    column_count = len(headers)
    if any(len(row) != column_count for row in body):
        return None

    rendered: list[str] = []
    primary_header = headers[0] or "Item"
    for row_index, row in enumerate(body, start=1):
        primary = row[0].strip() or f"Row {row_index}"
        rendered.append(f"**{primary_header}:** {primary}")
        for column_index, cell in enumerate(row[1:], start=1):
            value = cell.strip()
            if not value:
                continue
            rendered.append(f"• **{headers[column_index]}:** {value}")
        if row_index < len(body):
            rendered.append("")

    return "\n".join(rendered)


def _clean_table_cell(text: str) -> str:
    """Remove common Markdown markup from a table cell."""

    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    return text.replace("`", "").strip()


def _chunk_plain(text: str, limit: int) -> list[str]:
    """Split plain text into fixed-size chunks."""

    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _chunk_code_block(body: str, limit: int, language: str = "text") -> list[str]:
    """Split text into fenced code blocks that fit inside a platform limit."""

    fence_open = f"```{language}\n"
    fence_close = "\n```"
    available = max(1, limit - len(fence_open) - len(fence_close))
    escaped = _escape_code(body)
    chunks = []
    for i in range(0, len(escaped), available):
        part = escaped[i : i + available]
        chunks.append(f"{fence_open}{part}{fence_close}")
    return chunks or [f"{fence_open}{fence_close}"]
