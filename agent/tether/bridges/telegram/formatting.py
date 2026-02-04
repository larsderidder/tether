"""Telegram message formatting utilities."""


def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2.

    Telegram MarkdownV2 requires escaping many special characters.
    """
    special_chars = ["_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


def chunk_message(text: str, limit: int = 4096) -> list[str]:
    """Split a message into chunks at Telegram's character limit.

    Args:
        text: Text to chunk.
        limit: Maximum characters per chunk (default 4096 for Telegram).

    Returns:
        List of text chunks.
    """
    if len(text) <= limit:
        return [text]

    chunks = []
    for i in range(0, len(text), limit):
        chunks.append(text[i:i + limit])
    return chunks
