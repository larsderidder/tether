"""Tests for Telegram formatting utilities."""

from tether.bridges.rich_output import (
    parse_output_segments,
    render_discord_messages,
    render_telegram_messages,
)
from tether.bridges.telegram.formatting import (
    chunk_message,
    escape_markdown,
    markdown_to_telegram_html,
    strip_tool_markers,
    _markdown_table_to_pre,
)


class TestMarkdownToTelegramHtml:
    """Test markdown_to_telegram_html conversion."""

    def test_bold_double_asterisk(self) -> None:
        assert "<b>bold</b>" in markdown_to_telegram_html("**bold**")

    def test_bold_double_underscore(self) -> None:
        assert "<b>bold</b>" in markdown_to_telegram_html("__bold__")

    def test_italic_single_asterisk(self) -> None:
        assert "<i>italic</i>" in markdown_to_telegram_html("*italic*")

    def test_italic_single_underscore(self) -> None:
        assert "<i>italic</i>" in markdown_to_telegram_html("_italic_")

    def test_inline_code(self) -> None:
        assert "<code>foo</code>" in markdown_to_telegram_html("`foo`")

    def test_fenced_code_block(self) -> None:
        md = "```python\nprint('hello')\n```"
        result = markdown_to_telegram_html(md)
        assert "<pre>" in result
        assert "print" in result
        assert "</pre>" in result

    def test_code_block_language_stripped(self) -> None:
        md = "```js\nconsole.log('hi')\n```"
        result = markdown_to_telegram_html(md)
        assert "js" not in result.split("<pre>")[0]  # language tag not in output before <pre>

    def test_link(self) -> None:
        result = markdown_to_telegram_html("[click](https://example.com)")
        assert '<a href="https://example.com">click</a>' in result

    def test_header_becomes_bold(self) -> None:
        assert "<b>Title</b>" in markdown_to_telegram_html("# Title")
        assert "<b>Sub</b>" in markdown_to_telegram_html("## Sub")
        assert "<b>Deep</b>" in markdown_to_telegram_html("### Deep")

    def test_html_entities_escaped(self) -> None:
        result = markdown_to_telegram_html("a < b & c > d")
        assert "&lt;" in result
        assert "&amp;" in result
        assert "&gt;" in result

    def test_mixed_formatting(self) -> None:
        md = "**bold** and *italic* and `code`"
        result = markdown_to_telegram_html(md)
        assert "<b>bold</b>" in result
        assert "<i>italic</i>" in result
        assert "<code>code</code>" in result

    def test_nested_bold_in_header(self) -> None:
        # Headers convert to bold; if header text has bold, it still works
        result = markdown_to_telegram_html("# My Title")
        assert "<b>My Title</b>" in result

    def test_empty_string(self) -> None:
        assert markdown_to_telegram_html("") == ""

    def test_plain_text_unchanged(self) -> None:
        result = markdown_to_telegram_html("just plain text")
        assert "just plain text" in result


class TestMarkdownTableToPre:
    """Test _markdown_table_to_pre conversion."""

    def test_simple_table(self) -> None:
        table = "| Name | Age |\n|------|-----|\n| Alice | 30 |\n| Bob | 25 |"
        result = _markdown_table_to_pre(table)
        assert "<pre>" in result
        assert "</pre>" in result
        assert "Alice" in result
        assert "Bob" in result

    def test_separator_row_removed(self) -> None:
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = _markdown_table_to_pre(table)
        assert "---" not in result

    def test_columns_aligned(self) -> None:
        table = "| Short | LongColumnName |\n|-------|----------------|\n| a | b |"
        result = _markdown_table_to_pre(table)
        # Both data rows should be in <pre>
        assert "<pre>" in result

    def test_no_table_passthrough(self) -> None:
        text = "This is not a table\nJust regular text"
        assert _markdown_table_to_pre(text) == text

    def test_table_in_context(self) -> None:
        text = "Before table\n| A | B |\n|---|---|\n| 1 | 2 |\nAfter table"
        result = _markdown_table_to_pre(text)
        assert "Before table" in result
        assert "After table" in result
        assert "<pre>" in result

    def test_html_escaped_content(self) -> None:
        import html
        table = html.escape("| A<b> | B |\n|------|---|\n| 1 | 2 |")
        # Already escaped, should not break
        result = _markdown_table_to_pre(table)
        assert "&lt;" in result or "<pre>" in result


class TestStripToolMarkers:
    """Test strip_tool_markers removal."""

    def test_removes_tool_markers(self) -> None:
        text = "Some output\n[tool: Read]\nMore output"
        result = strip_tool_markers(text)
        assert "[tool: Read]" not in result
        assert "Some output" in result
        assert "More output" in result

    def test_removes_multiple_markers(self) -> None:
        text = "[tool: Read]\nOutput\n[tool: Edit]\nMore output\n[tool: Write]"
        result = strip_tool_markers(text)
        assert "[tool:" not in result
        assert "Output" in result
        assert "More output" in result

    def test_preserves_inline_tool_references(self) -> None:
        text = "The agent used [tool: Read] to check files"
        result = strip_tool_markers(text)
        # This is inline (not a full line), so it should NOT be removed
        assert "tool" in result

    def test_empty_string(self) -> None:
        assert strip_tool_markers("") == ""

    def test_no_markers(self) -> None:
        text = "Just normal text\nWith newlines"
        assert strip_tool_markers(text) == text

    def test_strips_whitespace(self) -> None:
        text = "\n[tool: Read]\n\n"
        result = strip_tool_markers(text)
        assert result == ""


class TestChunkMessage:
    """Test chunk_message splitting."""

    def test_short_message_no_split(self) -> None:
        assert chunk_message("short") == ["short"]

    def test_exact_limit(self) -> None:
        text = "x" * 4096
        assert chunk_message(text) == [text]

    def test_over_limit_splits(self) -> None:
        text = "x" * 5000
        chunks = chunk_message(text)
        assert len(chunks) == 2
        assert len(chunks[0]) == 4096
        assert len(chunks[1]) == 904

    def test_custom_limit(self) -> None:
        text = "x" * 100
        chunks = chunk_message(text, limit=30)
        assert len(chunks) == 4
        assert all(len(c) <= 30 for c in chunks)

    def test_chunks_concatenate_to_original(self) -> None:
        text = "a" * 10000
        chunks = chunk_message(text)
        assert "".join(chunks) == text


class TestEscapeMarkdown:
    """Test escape_markdown for MarkdownV2."""

    def test_escapes_special_chars(self) -> None:
        result = escape_markdown("hello_world")
        assert "\\_" in result

    def test_escapes_asterisk(self) -> None:
        result = escape_markdown("*bold*")
        assert "\\*" in result

    def test_escapes_brackets(self) -> None:
        result = escape_markdown("[link](url)")
        assert "\\[" in result
        assert "\\(" in result


class TestRichOutputFormatting:
    """Test semantic formatting for bridge output."""

    def test_parse_output_segments_classifies_tool_blocks(self) -> None:
        segments = parse_output_segments(
            "[tool: bash]\n"
            "[bash] pwd\n"
            "/tmp/demo\n"
            "[result] ok"
        )

        assert [segment.kind for segment in segments] == [
            "tool_call",
            "tool_output",
            "result",
        ]
        assert segments[1].label == "bash"
        assert "/tmp/demo" in segments[1].text

    def test_parse_output_segments_keeps_assistant_text_plain(self) -> None:
        segments = parse_output_segments("Final answer")

        assert [segment.kind for segment in segments] == ["assistant"]
        assert segments[0].text == "Final answer"

    def test_render_discord_messages_wraps_tool_output_in_code_block(self) -> None:
        messages = render_discord_messages("[tool: bash]\n[bash] pwd\n/tmp/demo")

        assert messages[0] == "🔧 **Tool call** `bash`"
        assert messages[1].startswith("📥 **Tool output** `bash`\n```text\n")
        assert "/tmp/demo" in messages[1]

    def test_render_discord_messages_normalizes_markdown_lists(self) -> None:
        messages = render_discord_messages(
            "Summary:\n- first item\n- second item\n1. third item\n2. fourth item"
        )

        assert messages == [
            "Summary:\n• first item\n• second item\n1) third item\n2) fourth item"
        ]

    def test_render_telegram_messages_formats_tool_output_as_pre(self) -> None:
        messages = render_telegram_messages("[error] invalid_grant")

        assert messages == [
            "⚠️ <b>Tool error</b>\n<pre>invalid_grant</pre>"
        ]

    def test_render_discord_messages_splits_explicit_assistant_marker(self) -> None:
        messages = render_discord_messages(
            "[notify] loki extension: no targets configured\n"
            "[assistant] Perfect. 👌 I am ready when you are. Send the first issue and I will jump in"
        )

        assert messages[0].startswith("📥 **Tool output** `notify`\n```text\n")
        assert "loki extension: no targets configured" in messages[0]
        assert messages[1] == (
            "Perfect. 👌 I am ready when you are. Send the first issue and I will jump in"
        )
