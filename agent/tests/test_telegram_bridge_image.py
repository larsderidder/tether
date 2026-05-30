"""Tests for Telegram bridge image collection."""

from __future__ import annotations

import base64

import pytest

from tether.bridges.telegram.bot import TelegramBridge

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 16)


class FakeTelegramFile:
    """Fake Telegram file download object."""

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(PNG_BYTES)


class FakeDocument:
    """Fake Telegram image document."""

    mime_type = "image/png"
    file_name = "diagram.png"
    file_size = len(PNG_BYTES)

    async def get_file(self) -> FakeTelegramFile:
        return FakeTelegramFile()


class FakeMessage:
    """Fake Telegram message with an image document."""

    photo: list[object] = []
    document = FakeDocument()
    replies: list[str]

    def __init__(self) -> None:
        self.replies = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class FakeUpdate:
    """Fake Telegram update wrapper."""

    def __init__(self, message: FakeMessage) -> None:
        self.message = message


@pytest.mark.anyio
async def test_collect_message_images_accepts_image_documents() -> None:
    """Telegram image documents are forwarded as native image input."""

    bridge = TelegramBridge("token", 123)
    message = FakeMessage()

    images = await bridge._collect_message_images(FakeUpdate(message))

    assert images == [
        {
            "type": "image",
            "data": base64.b64encode(PNG_BYTES).decode("ascii"),
            "mimeType": "image/png",
            "filename": "diagram.png",
        }
    ]
    assert message.replies == []
