"""Tests for inbound bridge dedupe and bot-loop suppression."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent_tether.base import BridgeCallbacks

from tether.bridges.dedupe import ShortLivedMessageDedupe
from tether.bridges.discord.bot import DiscordBridge
from tether.bridges.telegram.bot import TelegramBridge


def _mock_callbacks() -> BridgeCallbacks:
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


def test_short_lived_dedupe_expires_keys() -> None:
    """Dedupe keys are suppressed only inside the configured TTL."""

    now = 100.0
    dedupe = ShortLivedMessageDedupe(ttl_s=5.0, clock=lambda: now)

    assert dedupe.seen_recently("msg-1") is False
    assert dedupe.seen_recently("msg-1") is True

    now = 106.0
    assert dedupe.seen_recently("msg-1") is False


def test_discord_bridge_suppresses_duplicate_inbound_messages() -> None:
    """Repeated Discord delivery of the same message id is ignored."""

    bridge = DiscordBridge("token", 123, callbacks=_mock_callbacks())
    message = SimpleNamespace(
        id=42,
        content="hello",
        webhook_id=None,
        author=SimpleNamespace(id=7, bot=False),
        channel=SimpleNamespace(id=99),
        attachments=[],
    )

    assert bridge._should_ignore_inbound_message(message) is False
    assert bridge._should_ignore_inbound_message(message) is True


def test_discord_bridge_suppresses_bot_and_webhook_loops() -> None:
    """Obvious Discord bot or webhook echoes are not forwarded."""

    bridge = DiscordBridge("token", 123, callbacks=_mock_callbacks())
    bot_message = SimpleNamespace(
        id=1,
        webhook_id=None,
        author=SimpleNamespace(id=7, bot=True),
        channel=SimpleNamespace(id=99),
    )
    webhook_message = SimpleNamespace(
        id=2,
        webhook_id=123,
        author=SimpleNamespace(id=8, bot=False),
        channel=SimpleNamespace(id=99),
    )

    assert bridge._should_ignore_inbound_message(bot_message) is True
    assert bridge._should_ignore_inbound_message(webhook_message) is True


def test_telegram_bridge_suppresses_duplicate_inbound_media() -> None:
    """Repeated Telegram delivery of the same media message is ignored."""

    bridge = TelegramBridge("token", 123, callbacks=_mock_callbacks())
    update = SimpleNamespace(
        message=SimpleNamespace(
            message_id=42,
            media_group_id=None,
            caption="hello",
            from_user=SimpleNamespace(is_bot=False),
            via_bot=None,
            chat=SimpleNamespace(id=99),
            document=SimpleNamespace(file_unique_id="file-1", file_name="image.png"),
        )
    )

    assert bridge._should_ignore_inbound_media(update) is False
    assert bridge._should_ignore_inbound_media(update) is True


def test_telegram_bridge_suppresses_bot_loops() -> None:
    """Telegram bot-authored media is not forwarded back into agents."""

    bridge = TelegramBridge("token", 123, callbacks=_mock_callbacks())
    update = SimpleNamespace(
        message=SimpleNamespace(
            message_id=42,
            from_user=SimpleNamespace(is_bot=True),
            via_bot=None,
            chat=SimpleNamespace(id=99),
        )
    )

    assert bridge._should_ignore_inbound_media(update) is True
