"""Closing a Telegram topic detaches delivery without stopping the agent."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_tether.manager import BridgeManager
from telegram import Chat, ForumTopicClosed, Message, Update, User
from telegram.ext import ApplicationHandlerStop

from tether.bridges.subscriber import BridgeSubscriber
from tether.bridges.telegram.bot import TelegramBridge
from tether.bridges.telegram.state import StateManager
from tether.external_session_watcher import external_session_watcher
from tether.models import SessionState
from tether.store import SessionStore


@pytest.mark.anyio
async def test_closing_topic_detaches_and_discards_pending_output(
    fresh_store: SessionStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closure persists detachment, cancels queued sends, and leaves the runner alone."""
    session = fresh_store.create_session(str(tmp_path), None)
    session.platform = "telegram"
    session.platform_thread_id = "123"
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)
    bridge = TelegramBridge(
        bot_token="test_token",
        forum_group_id=-1001234567890,
        state_manager=StateManager(str(tmp_path / "telegram_state.json")),
    )
    bridge._state.set_topic_for_session(session.id, 123, "Test")
    bridge._app = SimpleNamespace(bot=AsyncMock())
    manager = BridgeManager()
    manager.register_bridge("telegram", bridge)
    subscriber = BridgeSubscriber(
        bridge_manager=manager,
        new_subscriber=fresh_store.new_subscriber,
        remove_subscriber=fresh_store.remove_subscriber,
    )
    monkeypatch.setattr("tether.bridges.glue.bridge_subscriber", subscriber)
    subscriber.subscribe(session.id, "telegram")
    subscriber._buffer_output(session.id, "Buffered output")
    sent = fresh_store.enqueue_bridge_delivery(
        session_id=session.id,
        platform="telegram",
        delivery_key="sent",
        method="on_output",
        payload={"text": "Already delivered"},
    )
    fresh_store.mark_bridge_delivery_sent(sent.id)
    queued = fresh_store.enqueue_bridge_delivery(
        session_id=session.id,
        platform="telegram",
        delivery_key="queued",
        method="on_output",
        payload={"text": "Queued output"},
    )
    fresh_store.claim_bridge_delivery(queued.id)
    external_session_watcher.register(session.id)
    # Anonymous admins and bots can originate authoritative topic service messages.
    update = Update(
        1,
        message=Message(
            message_id=1,
            date=0,
            chat=Chat(-1001234567890, "supergroup"),
            from_user=User(1087968824, "Group", is_bot=True),
            message_thread_id=123,
            forum_topic_closed=ForumTopicClosed(),
        ),
    )

    with pytest.raises(ApplicationHandlerStop):
        await bridge._guard_update(update, None)
    await asyncio.sleep(0)

    restored = SessionStore().get_session(session.id)
    assert restored is not None
    assert restored.platform is None
    assert restored.platform_thread_id is None
    assert restored.state == SessionState.RUNNING
    assert session.id not in subscriber._tasks
    assert session.id not in external_session_watcher._session_ids
    assert fresh_store.claim_bridge_delivery(queued.id).status == "cancelled"
    assert fresh_store.claim_bridge_delivery(sent.id).status == "sent"
    state = StateManager(str(tmp_path / "telegram_state.json"))
    state.load()
    assert state.get_session_for_topic(123) is None
    bridge._app.bot.send_message.assert_not_awaited()
    bridge._app.bot.send_chat_action.assert_not_awaited()


@pytest.mark.anyio
async def test_foreign_chat_closure_cannot_detach_topic(tmp_path: Path) -> None:
    """Telegram thread IDs are scoped to a chat, not globally unique."""
    bridge = TelegramBridge(
        bot_token="test_token",
        forum_group_id=-1001234567890,
        state_manager=StateManager(str(tmp_path / "telegram_state.json")),
    )
    bridge._state.set_topic_for_session("session", 123, "Test")
    bridge._detach_closed_topic = AsyncMock()
    update = Update(
        1,
        message=Message(
            message_id=1,
            date=0,
            chat=Chat(-1009999999999, "supergroup"),
            from_user=User(1234, "Human", is_bot=False),
            message_thread_id=123,
            forum_topic_closed=ForumTopicClosed(),
        ),
    )

    await bridge._guard_update(update, None)

    bridge._detach_closed_topic.assert_not_awaited()
    assert bridge._state.get_topic_for_session("session") == 123


@pytest.mark.anyio
async def test_output_waiting_for_pacing_does_not_send_after_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recheck the topic binding after a flood-control or pacing delay."""
    bridge = TelegramBridge(
        bot_token="test_token",
        forum_group_id=-1001234567890,
        state_manager=StateManager(str(tmp_path / "telegram_state.json")),
    )
    bridge._state.set_topic_for_session("session", 123, "Test")
    bridge._app = SimpleNamespace(bot=AsyncMock())
    bridge._output_paused_until = asyncio.get_running_loop().time() + 30

    async def close_while_waiting(_delay: float) -> None:
        bridge._state.remove_session("session")

    monkeypatch.setattr(
        "tether.bridges.telegram.bot.asyncio.sleep", close_while_waiting
    )
    with pytest.raises(RuntimeError, match="no longer bound"):
        await bridge._send_output_message("session", 123, "Too late")
    bridge._app.bot.send_message.assert_not_awaited()


@pytest.mark.anyio
async def test_closing_old_topic_cannot_detach_new_binding(tmp_path: Path) -> None:
    """Ignore a leftover reverse mapping after the session moved to another topic."""
    bridge = TelegramBridge(
        bot_token="test_token",
        forum_group_id=-1001234567890,
        state_manager=StateManager(str(tmp_path / "telegram_state.json")),
    )
    bridge._state.set_topic_for_session("session", 123, "Old")
    bridge._state.set_topic_for_session("session", 456, "New")

    await bridge._detach_closed_topic(123)

    assert bridge._state.get_topic_for_session("session") == 456
