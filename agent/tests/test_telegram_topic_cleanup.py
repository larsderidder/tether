"""Tests for closing orphaned Telegram topics without deleting their history."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter

from tether.bridges.telegram.bot import TelegramBridge
from tether.bridges.telegram.state import StateManager


@pytest.fixture
def bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TelegramBridge:
    """Use real persisted mappings and mock only Telegram calls and pacing."""
    state = StateManager(str(tmp_path / "telegram_state.json"))
    result = TelegramBridge(
        bot_token="test_token",
        forum_group_id=-1001234567890,
        state_manager=state,
    )
    result._app = MagicMock()
    result._app.bot.close_forum_topic = AsyncMock(return_value=True)
    result._app.bot.delete_forum_topic = AsyncMock()
    monkeypatch.setattr("tether.bridges.telegram.bot.asyncio.sleep", AsyncMock())
    return result


@pytest.mark.anyio
async def test_close_orphan_preserves_history_and_persists_cleanup(
    bridge: TelegramBridge,
) -> None:
    """Successful closure forgets the mapping, never the Telegram history."""
    bridge._state.set_topic_for_session("gone", 123, "Old session")

    assert await bridge.close_orphaned_topics(lambda: []) == 1

    bridge._app.bot.close_forum_topic.assert_awaited_once_with(
        chat_id=-1001234567890, message_thread_id=123
    )
    bridge._app.bot.delete_forum_topic.assert_not_awaited()
    restored = StateManager(str(bridge._state._path))
    restored.load()
    assert restored.get_session_for_topic(123) is None
    assert await bridge.close_orphaned_topics(lambda: []) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["telegram", None])
async def test_retained_session_is_not_closed_even_when_detached(
    bridge: TelegramBridge, platform: str | None
) -> None:
    """An existing session is not an orphan, regardless of platform binding."""
    bridge._state.set_topic_for_session("live", 123, "Retained session")
    sessions = [{"id": "live", "platform": platform, "platform_thread_id": "123"}]

    assert await bridge.close_orphaned_topics(lambda: sessions) == 0
    bridge._app.bot.close_forum_topic.assert_not_awaited()
    assert bridge._state.get_session_for_topic(123) == "live"


@pytest.mark.anyio
async def test_reused_topic_is_not_closed_for_an_old_mapping(
    bridge: TelegramBridge,
) -> None:
    """Another session's binding protects a reused topic."""
    bridge._state.set_topic_for_session("gone", 123, "Old session")
    sessions = [{"id": "new", "platform": "telegram", "platform_thread_id": "123"}]

    assert await bridge.close_orphaned_topics(lambda: sessions) == 0
    bridge._app.bot.close_forum_topic.assert_not_awaited()


@pytest.mark.anyio
async def test_general_topic_is_never_closed(bridge: TelegramBridge) -> None:
    """Even a stale mapping cannot close the General topic."""
    bridge._state.set_topic_for_session("gone", 1, "General")

    assert await bridge.close_orphaned_topics(lambda: []) == 0
    bridge._app.bot.close_forum_topic.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "message", ["Topic_not_modified", "Topic_id_invalid", "Message thread not found"]
)
async def test_already_closed_or_missing_topic_is_retired(
    bridge: TelegramBridge, message: str
) -> None:
    """Already-resolved topics do not cause an endless retry loop."""
    bridge._state.set_topic_for_session("gone", 123, "Old session")
    bridge._app.bot.close_forum_topic.side_effect = BadRequest(message)

    assert await bridge.close_orphaned_topics(lambda: []) == 1
    assert bridge._state.get_session_for_topic(123) is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [Forbidden("Not enough rights"), BadRequest("Chat not found")]
)
async def test_failed_close_keeps_mapping_for_retry(
    bridge: TelegramBridge, error: Exception
) -> None:
    """An unsuccessful Telegram request must not lose the pending mapping."""
    bridge._state.set_topic_for_session("gone", 123, "Old session")
    bridge._app.bot.close_forum_topic.side_effect = error

    assert await bridge.close_orphaned_topics(lambda: []) == 0
    assert bridge._state.get_session_for_topic(123) == "gone"


@pytest.mark.anyio
async def test_rate_limit_defers_remaining_topics_without_losing_mappings(
    bridge: TelegramBridge,
) -> None:
    """Flood control pauses the batch, then permits a later retry."""
    bridge._state.set_topic_for_session("first", 123, "First")
    bridge._state.set_topic_for_session("second", 456, "Second")
    bridge._app.bot.close_forum_topic.side_effect = RetryAfter(120)

    assert await bridge.close_orphaned_topics(lambda: []) == 0
    assert await bridge.close_orphaned_topics(lambda: []) == 0
    bridge._app.bot.close_forum_topic.assert_awaited_once()
    assert bridge._state.get_session_for_topic(123) == "first"
    assert bridge._state.get_session_for_topic(456) == "second"

    bridge._topic_cleanup_paused_until = 0
    bridge._app.bot.close_forum_topic.side_effect = None
    assert await bridge.close_orphaned_topics(lambda: []) == 2


@pytest.mark.anyio
async def test_session_list_failure_does_not_close_anything(
    bridge: TelegramBridge,
) -> None:
    """Store failures are not interpreted as an empty list of sessions."""
    bridge._state.set_topic_for_session("gone", 123, "Old session")
    get_sessions = MagicMock(side_effect=RuntimeError("Store unavailable"))

    with pytest.raises(RuntimeError, match="Store unavailable"):
        await bridge.close_orphaned_topics(get_sessions)

    bridge._app.bot.close_forum_topic.assert_not_awaited()


@pytest.mark.anyio
async def test_rechecks_live_sessions_between_closures(bridge: TelegramBridge) -> None:
    """Sessions restored during a cleanup pass remain protected."""
    bridge._state.set_topic_for_session("first", 123, "First")
    bridge._state.set_topic_for_session("second", 456, "Second")
    get_sessions = MagicMock(side_effect=[[], [{"id": "second"}]])

    assert await bridge.close_orphaned_topics(get_sessions) == 1
    bridge._app.bot.close_forum_topic.assert_awaited_once()
    assert bridge._state.get_session_for_topic(456) == "second"


@pytest.mark.anyio
async def test_cancellation_preserves_pending_mapping(bridge: TelegramBridge) -> None:
    """Shutdown propagates cancellation without dropping pending work."""
    bridge._state.set_topic_for_session("gone", 123, "Old session")
    bridge._app.bot.close_forum_topic.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await bridge.close_orphaned_topics(lambda: [])

    assert bridge._state.get_session_for_topic(123) == "gone"
