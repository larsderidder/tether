"""Directory history and Telegram session/model picker behavior."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent_tether.base import BridgeCallbacks, BridgeConfig

from tether.bridges.session_creation import recent_directories
from tether.bridges.telegram.bot import TelegramBridge


@pytest.fixture
def callbacks() -> BridgeCallbacks:
    """Keep all host calls local and observable."""
    return BridgeCallbacks(
        create_session=AsyncMock(
            return_value={
                "id": "sess_new",
                "adapter": "pi_rpc",
                "model": "provider/model",
            }
        ),
        send_input=AsyncMock(),
        stop_session=AsyncMock(),
        respond_to_permission=AsyncMock(),
        list_sessions=AsyncMock(return_value=[]),
        get_usage=AsyncMock(),
        check_directory=AsyncMock(
            side_effect=lambda path: {"exists": True, "path": path}
        ),
        list_external_sessions=AsyncMock(return_value=[]),
        get_external_history=AsyncMock(),
        attach_external=AsyncMock(),
    )


@pytest.fixture
def bridge(callbacks: BridgeCallbacks, tmp_path: Path) -> TelegramBridge:
    """Use an isolated state manager and no Telegram client."""
    bridge = TelegramBridge(
        bot_token="test_token",
        forum_group_id=-100123,
        config=BridgeConfig(default_adapter="pi_rpc", data_dir=str(tmp_path)),
        callbacks=callbacks,
    )
    bridge._allowed_user_ids = set()
    bridge._state = MagicMock()
    bridge._state.get_session_for_topic.return_value = None
    bridge.on_output = AsyncMock()
    return bridge


def command_update(topic_id: int | None = None) -> SimpleNamespace:
    """Create a Telegram command with a real user identity and reply sink."""
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(
            chat_id=-100123, message_thread_id=topic_id, reply_text=AsyncMock()
        ),
    )


def callback_update(
    update: SimpleNamespace, data: str, user_id: int = 42
) -> SimpleNamespace:
    """Click a button in the command's chat and topic."""
    user = SimpleNamespace(id=user_id)
    return SimpleNamespace(
        effective_user=user,
        callback_query=SimpleNamespace(
            data=data,
            message=update.message,
            from_user=user,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        ),
    )


@pytest.mark.anyio
async def test_history_merges_external_sessions_and_skips_missing_directories(
    callbacks, tmp_path
):
    """Use the newest activity per canonical directory, not the newest list position."""
    older = tmp_path / "older"
    newer = tmp_path / "newer"
    older.mkdir()
    newer.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(older, target_is_directory=True)
    callbacks.list_sessions.return_value = [
        {"directory": str(older), "last_activity_at": "2026-01-01T00:00:00Z"},
        {"directory": str(newer), "last_activity_at": "2026-01-02T00:00:00Z"},
        {"directory": str(tmp_path / "gone")},
    ]
    callbacks.list_external_sessions.return_value = [
        {"directory": str(alias), "last_activity": "2026-01-03T00:00:00Z"}
    ]

    assert await recent_directories(callbacks) == [str(older), str(newer)]


@pytest.mark.anyio
async def test_history_survives_external_discovery_failure(callbacks, tmp_path):
    """Local history stays usable when an external session source is unavailable."""
    callbacks.list_sessions.return_value = [{"directory": str(tmp_path)}]
    callbacks.list_external_sessions.side_effect = RuntimeError("unavailable")
    assert await recent_directories(callbacks) == [str(tmp_path)]


@pytest.mark.anyio
async def test_new_offers_directory_buttons_and_creates_once(
    bridge, callbacks, tmp_path
):
    """Selecting a long path with spaces revalidates it without embedding it in callback data."""
    directory = tmp_path / ("long project " * 10).rstrip()
    directory.mkdir()
    callbacks.list_sessions.return_value = [{"directory": str(directory)}]
    update = command_update()
    await bridge._cmd_new(update, SimpleNamespace(args=[]))
    callbacks.create_session.assert_not_awaited()
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    data = markup.inline_keyboard[0][0].callback_data
    assert len(data.encode()) <= 64
    assert str(directory) in update.message.reply_text.await_args.args[0]

    click = callback_update(update, data)
    await bridge._handle_callback_query(click, None)
    await bridge._handle_callback_query(click, None)

    callbacks.create_session.assert_awaited_once()
    assert callbacks.create_session.await_args.kwargs["directory"] == str(directory)
    callbacks.check_directory.assert_awaited_once_with(str(directory))
    bridge.on_output.assert_awaited_once_with(
        "sess_new", "Agent: Pi\nModel: provider/model"
    )


@pytest.mark.anyio
async def test_picker_is_bound_to_its_user_and_expires(bridge, callbacks, tmp_path):
    """Another user cannot select from a menu, nor can its owner use expired choices."""
    callbacks.list_sessions.return_value = [{"directory": str(tmp_path)}]
    update = command_update()
    await bridge._cmd_new(update, SimpleNamespace(args=[]))
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    data = markup.inline_keyboard[0][0].callback_data
    await bridge._handle_callback_query(callback_update(update, data, user_id=43), None)
    callbacks.create_session.assert_not_awaited()
    next(iter(bridge._session_pickers.values())).expires_at = 0
    click = callback_update(update, data)
    await bridge._handle_callback_query(click, None)
    assert "expired" in click.callback_query.answer.await_args.args[0]
    callbacks.create_session.assert_not_awaited()


@pytest.mark.anyio
async def test_picker_pagination_keeps_snapshot(bridge, callbacks, tmp_path):
    """Paging never refreshes the list under existing numbered choices."""
    directories = []
    for index in range(8):
        directory = tmp_path / str(index)
        directory.mkdir()
        directories.append(str(directory))
    callbacks.list_sessions.return_value = [{"directory": path} for path in directories]
    update = command_update()
    await bridge._cmd_new(update, SimpleNamespace(args=["pi"]))
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    data = markup.inline_keyboard[-2][0].callback_data
    click = callback_update(update, data)
    await bridge._handle_callback_query(click, None)
    assert "Page 2/2" in click.callback_query.edit_message_text.await_args.args[0]
    callbacks.list_sessions.assert_awaited_once()


@pytest.mark.anyio
async def test_model_picker_retains_choices_on_failure(bridge, monkeypatch):
    """A rejected model produces an actionable reply and leaves the picker usable."""
    bridge._state.get_session_for_topic.return_value = "sess_current"
    monkeypatch.setattr(
        "tether.bridges.telegram.bot.get_session_model",
        AsyncMock(
            return_value={
                "adapter": "pi_rpc",
                "model": "provider/old",
                "available_models": ["provider/new"],
            }
        ),
    )
    change_model = AsyncMock(
        side_effect=ValueError("Model unavailable; previous model unchanged")
    )
    monkeypatch.setattr("tether.bridges.telegram.bot.set_session_model", change_model)
    update = command_update(topic_id=10)
    await bridge._cmd_model(update, SimpleNamespace(args=[]))
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    data = markup.inline_keyboard[0][0].callback_data
    click = callback_update(update, data)
    await bridge._handle_callback_query(click, None)
    assert "previous model unchanged" in update.message.reply_text.await_args.args[0]
    assert bridge._session_pickers

    change_model.side_effect = None
    change_model.return_value = {"model": "provider/new"}
    await bridge._handle_callback_query(click, None)
    assert not bridge._session_pickers
    assert "provider/new" in click.callback_query.edit_message_text.await_args.args[0]


@pytest.mark.anyio
async def test_notice_failure_does_not_repeat_successful_creation(bridge, callbacks):
    """Notification delivery cannot cause the caller to create another session."""
    bridge.on_output.side_effect = RuntimeError("Telegram unavailable")
    session = await bridge._create_session_via_api(
        directory="/project", platform="telegram"
    )
    assert session["id"] == "sess_new"
    callbacks.create_session.assert_awaited_once()
