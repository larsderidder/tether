from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_tether.base import BridgeCallbacks


def _mock_callbacks(**overrides) -> BridgeCallbacks:
    defaults = dict(
        create_session=AsyncMock(return_value={}),
        send_input=AsyncMock(),
        stop_session=AsyncMock(),
        respond_to_permission=AsyncMock(return_value=True),
        list_sessions=AsyncMock(return_value=[]),
        get_usage=AsyncMock(return_value={}),
        check_directory=AsyncMock(side_effect=lambda path: {"exists": True, "path": path}),
        list_external_sessions=AsyncMock(return_value=[]),
        get_external_history=AsyncMock(return_value=None),
        attach_external=AsyncMock(return_value={}),
    )
    defaults.update(overrides)
    return BridgeCallbacks(**defaults)


@pytest.mark.anyio
async def test_discord_checkmark_reaction_creates_and_starts_session_from_control_channel_message() -> None:
    from tether.bridges.discord.bot import DiscordBridge, DiscordConfig

    callbacks = _mock_callbacks(
        create_session=AsyncMock(
            return_value={"id": "sess_discord", "platform_thread_id": "333"}
        ),
    )
    bridge = DiscordBridge(
        bot_token="discord_bot_token",
        channel_id=1234567890,
        discord_config=DiscordConfig(
            reaction_new_session_enabled=True,
            reaction_new_session_emoji="✅",
        ),
        callbacks=callbacks,
    )

    control_channel = AsyncMock()
    control_channel.id = 1234567890
    source_message = MagicMock()
    source_message.content = "!new codex /repo\nFix the Discord reaction flow."
    source_message.author.bot = False
    control_channel.fetch_message = AsyncMock(return_value=source_message)

    mock_client = MagicMock()
    mock_client.user = MagicMock(id=9999)
    mock_client.get_channel.return_value = control_channel
    bridge._client = mock_client

    payload = MagicMock()
    payload.channel_id = 1234567890
    payload.message_id = 4444
    payload.user_id = 1111
    payload.emoji = MagicMock()
    payload.emoji.name = "✅"

    await bridge._handle_raw_reaction_add(payload)

    create_kwargs = callbacks.create_session.await_args.kwargs
    assert create_kwargs["directory"] == "/repo"
    assert create_kwargs["platform"] == "discord"
    assert create_kwargs["adapter"] == "codex_sdk_sidecar"
    assert callbacks.send_input.await_args.args == (
        "sess_discord",
        "Fix the Discord reaction flow.",
    )
    control_channel.send.assert_awaited()
    assert "<#333>" in control_channel.send.await_args.args[0]


@pytest.mark.anyio
async def test_discord_checkmark_reaction_creates_and_starts_session_from_plain_control_channel_message(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_tether.base import BridgeConfig
    from tether.bridges.discord.bot import DiscordBridge, DiscordConfig

    monkeypatch.chdir(tmp_path)
    callbacks = _mock_callbacks(
        create_session=AsyncMock(
            return_value={"id": "sess_discord_plain", "platform_thread_id": "333"}
        ),
    )
    bridge = DiscordBridge(
        bot_token="discord_bot_token",
        channel_id=1234567890,
        discord_config=DiscordConfig(
            reaction_new_session_enabled=True,
            reaction_new_session_emoji="✅",
            reaction_new_session_allow_plain_messages=True,
        ),
        callbacks=callbacks,
        config=BridgeConfig(default_adapter="codex_sdk_sidecar"),
    )

    control_channel = AsyncMock()
    control_channel.id = 1234567890
    source_message = MagicMock()
    source_message.content = "LATEST THINKPAD CHECKMARK TEST 1"
    source_message.author.bot = False
    control_channel.fetch_message = AsyncMock(return_value=source_message)

    mock_client = MagicMock()
    mock_client.user = MagicMock(id=9999)
    mock_client.get_channel.return_value = control_channel
    bridge._client = mock_client

    payload = MagicMock()
    payload.channel_id = 1234567890
    payload.message_id = 4445
    payload.user_id = 1111
    payload.emoji = MagicMock()
    payload.emoji.name = "✅"

    await bridge._handle_raw_reaction_add(payload)

    create_kwargs = callbacks.create_session.await_args.kwargs
    assert create_kwargs["directory"] == str(tmp_path)
    assert create_kwargs["platform"] == "discord"
    assert create_kwargs["adapter"] == "codex_sdk_sidecar"
    assert callbacks.send_input.await_args.args == (
        "sess_discord_plain",
        "LATEST THINKPAD CHECKMARK TEST 1",
    )
    control_channel.send.assert_awaited()
    assert "<#333>" in control_channel.send.await_args.args[0]


@pytest.mark.anyio
async def test_discord_checkmark_reaction_ignores_threads_unauthorized_users_and_duplicate_events(
    tmp_path,
) -> None:
    from agent_tether.base import BridgeConfig
    from tether.bridges.discord.bot import DiscordBridge, DiscordConfig

    callbacks = _mock_callbacks(
        create_session=AsyncMock(
            return_value={"id": "sess_discord", "platform_thread_id": "333"}
        ),
    )
    bridge = DiscordBridge(
        bot_token="discord_bot_token",
        channel_id=1234567890,
        discord_config=DiscordConfig(
            require_pairing=True,
            pairing_code="12345678",
            reaction_new_session_enabled=True,
        ),
        callbacks=callbacks,
        config=BridgeConfig(data_dir=str(tmp_path)),
    )

    thread_channel = AsyncMock()
    thread_channel.id = 999999999
    thread_message = MagicMock()
    thread_message.content = "!new codex /repo\nIgnore thread reactions."
    thread_message.author.bot = False
    thread_channel.fetch_message = AsyncMock(return_value=thread_message)

    control_channel = AsyncMock()
    control_channel.id = 1234567890
    control_message = MagicMock()
    control_message.content = "!new codex /repo\nBuild one session only."
    control_message.author.bot = False
    control_channel.fetch_message = AsyncMock(return_value=control_message)

    mock_client = MagicMock()
    mock_client.user = MagicMock(id=9999)
    mock_client.get_channel.side_effect = lambda channel_id: {
        1234567890: control_channel,
        999999999: thread_channel,
    }.get(channel_id)
    bridge._client = mock_client

    unauthorized_payload = MagicMock()
    unauthorized_payload.channel_id = 1234567890
    unauthorized_payload.message_id = 1001
    unauthorized_payload.user_id = 2002
    unauthorized_payload.emoji = MagicMock()
    unauthorized_payload.emoji.name = "✅"

    await bridge._handle_raw_reaction_add(unauthorized_payload)

    thread_payload = MagicMock()
    thread_payload.channel_id = 999999999
    thread_payload.message_id = 1002
    thread_payload.user_id = 2002
    thread_payload.emoji = MagicMock()
    thread_payload.emoji.name = "✅"

    await bridge._handle_raw_reaction_add(thread_payload)

    bridge._allowed_user_ids = [2002]
    valid_payload = MagicMock()
    valid_payload.channel_id = 1234567890
    valid_payload.message_id = 1003
    valid_payload.user_id = 2002
    valid_payload.emoji = MagicMock()
    valid_payload.emoji.name = "✅"

    await bridge._handle_raw_reaction_add(valid_payload)
    await bridge._handle_raw_reaction_add(valid_payload)

    assert callbacks.create_session.await_count == 1
    assert callbacks.send_input.await_count == 1
    assert thread_channel.fetch_message.await_count == 0


@pytest.mark.anyio
async def test_discord_multiline_new_message_waits_for_reaction_instead_of_running_command() -> None:
    from tether.bridges.discord.bot import DiscordBridge, DiscordConfig

    bridge = DiscordBridge(
        bot_token="discord_bot_token",
        channel_id=1234567890,
        discord_config=DiscordConfig(reaction_new_session_enabled=True),
        callbacks=_mock_callbacks(),
    )
    bridge._dispatch_command = AsyncMock()

    message = MagicMock()
    message.author.bot = False
    message.content = "!new codex /repo\nFix the Discord reaction flow."
    message.channel = MagicMock()
    message.channel.id = 1234567890

    await bridge._handle_message(message)

    bridge._dispatch_command.assert_not_awaited()


@pytest.mark.anyio
async def test_slack_checkmark_reaction_creates_and_starts_session_from_control_channel_message() -> None:
    from tether.bridges.slack.bot import SlackBridge

    callbacks = _mock_callbacks(
        create_session=AsyncMock(
            return_value={"id": "sess_slack", "platform_thread_id": "555.666"}
        ),
    )
    bridge = SlackBridge(
        bot_token="xoxb-test-token",
        channel_id="C01234567",
        callbacks=callbacks,
        reaction_new_session_enabled=True,
        reaction_new_session_emoji="✅",
    )

    mock_client = AsyncMock()
    mock_client.conversations_history.return_value = {
        "ok": True,
        "messages": [
            {
                "text": "!new codex /repo\nFix the Slack reaction flow.",
                "ts": "111.222",
            }
        ],
    }
    bridge._client = mock_client

    await bridge._handle_reaction_added(
        {
            "reaction": "white_check_mark",
            "item": {"channel": "C01234567", "ts": "111.222"},
            "user": "U123",
        }
    )

    create_kwargs = callbacks.create_session.await_args.kwargs
    assert create_kwargs["directory"] == "/repo"
    assert create_kwargs["platform"] == "slack"
    assert create_kwargs["adapter"] == "codex_sdk_sidecar"
    assert callbacks.send_input.await_args.args == (
        "sess_slack",
        "Fix the Slack reaction flow.",
    )
    assert mock_client.chat_postMessage.await_count == 1
    assert "New Codex session created in repo" in mock_client.chat_postMessage.await_args.kwargs["text"]


@pytest.mark.anyio
async def test_slack_checkmark_reaction_creates_and_starts_session_from_plain_control_channel_message(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_tether.base import BridgeConfig
    from tether.bridges.slack.bot import SlackBridge

    monkeypatch.chdir(tmp_path)
    callbacks = _mock_callbacks(
        create_session=AsyncMock(
            return_value={"id": "sess_slack_plain", "platform_thread_id": "555.666"}
        ),
    )
    bridge = SlackBridge(
        bot_token="xoxb-test-token",
        channel_id="C01234567",
        callbacks=callbacks,
        reaction_new_session_enabled=True,
        reaction_new_session_emoji="✅",
        reaction_new_session_allow_plain_messages=True,
        config=BridgeConfig(default_adapter="codex_sdk_sidecar"),
    )

    mock_client = AsyncMock()
    mock_client.conversations_history.return_value = {
        "ok": True,
        "messages": [
            {
                "text": "LATEST THINKPAD CHECKMARK TEST 1",
                "ts": "111.222",
            }
        ],
    }
    bridge._client = mock_client

    await bridge._handle_reaction_added(
        {
            "reaction": "white_check_mark",
            "item": {"channel": "C01234567", "ts": "111.222"},
            "user": "U123",
        }
    )

    create_kwargs = callbacks.create_session.await_args.kwargs
    assert create_kwargs["directory"] == str(tmp_path)
    assert create_kwargs["platform"] == "slack"
    assert create_kwargs["adapter"] == "codex_sdk_sidecar"
    assert callbacks.send_input.await_args.args == (
        "sess_slack_plain",
        "LATEST THINKPAD CHECKMARK TEST 1",
    )
    assert mock_client.chat_postMessage.await_count == 1
    assert (
        "New Codex session created in"
        in mock_client.chat_postMessage.await_args.kwargs["text"]
    )


@pytest.mark.anyio
async def test_slack_checkmark_reaction_ignores_non_checkmark_reactions_and_thread_messages() -> None:
    from tether.bridges.slack.bot import SlackBridge

    callbacks = _mock_callbacks(
        create_session=AsyncMock(
            return_value={"id": "sess_slack", "platform_thread_id": "555.666"}
        ),
    )
    bridge = SlackBridge(
        bot_token="xoxb-test-token",
        channel_id="C01234567",
        callbacks=callbacks,
        reaction_new_session_enabled=True,
    )

    mock_client = AsyncMock()
    mock_client.conversations_history.return_value = {
        "ok": True,
        "messages": [
            {
                "text": "!new codex /repo\nIgnore thread reactions.",
                "ts": "111.222",
                "thread_ts": "999.000",
            }
        ],
    }
    bridge._client = mock_client

    await bridge._handle_reaction_added(
        {
            "reaction": "eyes",
            "item": {"channel": "C01234567", "ts": "111.222"},
        }
    )
    await bridge._handle_reaction_added(
        {
            "reaction": "white_check_mark",
            "item": {"channel": "C01234567", "ts": "111.222"},
        }
    )
    await bridge._handle_reaction_added(
        {
            "reaction": "white_check_mark",
            "item": {"channel": "C01234567", "ts": "111.222"},
        }
    )

    assert callbacks.create_session.await_count == 0
    assert callbacks.send_input.await_count == 0


@pytest.mark.anyio
async def test_slack_multiline_new_message_waits_for_reaction_instead_of_running_command() -> None:
    from tether.bridges.slack.bot import SlackBridge

    bridge = SlackBridge(
        bot_token="xoxb-test-token",
        channel_id="C01234567",
        callbacks=_mock_callbacks(),
        reaction_new_session_enabled=True,
    )
    bridge._dispatch_command = AsyncMock()

    await bridge._handle_message(
        {
            "channel": "C01234567",
            "ts": "111.222",
            "text": "!new codex /repo\nFix the Slack reaction flow.",
        }
    )

    bridge._dispatch_command.assert_not_awaited()
