"""Tests for Telegram bridge image collection."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest

from agent_tether.base import BridgeCallbacks
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


class FakeChat:
    """Fake Telegram chat."""

    id = 456


class FakeMessage:
    """Fake Telegram message with an image document."""

    photo: list[object] = []
    document = FakeDocument()
    chat = FakeChat()
    replies: list[str]

    def __init__(self, caption: str | None = None) -> None:
        self.caption = caption
        self.replies = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class FakeUpdate:
    """Fake Telegram update wrapper."""

    def __init__(self, message: FakeMessage) -> None:
        self.message = message


def _mock_callbacks() -> BridgeCallbacks:
    """Create BridgeCallbacks with all methods mocked."""

    return BridgeCallbacks(
        create_session=AsyncMock(return_value={}),
        send_input=AsyncMock(),
        stop_session=AsyncMock(),
        respond_to_permission=AsyncMock(return_value=True),
        list_sessions=AsyncMock(return_value=[]),
        get_usage=AsyncMock(return_value={}),
        check_directory=AsyncMock(return_value={"exists": True, "path": "/tmp"}),
        list_external_sessions=AsyncMock(return_value=[]),
        get_external_history=AsyncMock(return_value=None),
        attach_external=AsyncMock(return_value={}),
    )


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


@pytest.mark.anyio
async def test_media_group_buffers_images_as_one_input() -> None:
    """Telegram media groups are forwarded as a single image turn."""

    callbacks = _mock_callbacks()
    bridge = TelegramBridge("token", 123, callbacks=callbacks)
    first = FakeMessage(caption="first")
    second = FakeMessage(caption="second")

    await bridge._buffer_media_group(FakeUpdate(first), "sess1", 99, "album1")
    await bridge._buffer_media_group(FakeUpdate(second), "sess1", 99, "album1")
    await bridge._flush_media_group("456:99:album1")

    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    callbacks.send_input.assert_awaited_once_with(
        "sess1",
        "first\nsecond",
        images=[
            {
                "type": "image",
                "data": encoded,
                "mimeType": "image/png",
                "filename": "diagram.png",
            },
            {
                "type": "image",
                "data": encoded,
                "mimeType": "image/png",
                "filename": "diagram.png",
            },
        ],
    )
