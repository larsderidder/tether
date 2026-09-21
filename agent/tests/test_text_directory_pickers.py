"""Exercise numbered directory choices through the real text bridge parsers."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_tether.base import BridgeCallbacks, BridgeConfig

from tether.bridges.discord.bot import DiscordBridge
from tether.bridges.slack.bot import SlackBridge


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["slack", "discord"])
async def test_bare_new_and_numbered_choice_preserve_path_with_spaces(
    platform: str, tmp_path: Path
) -> None:
    """Default-agent choices create exactly the displayed directory and announce configuration."""
    directory = tmp_path / "project with spaces"
    directory.mkdir()
    callbacks = BridgeCallbacks(
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
        list_sessions=AsyncMock(return_value=[{"directory": str(directory)}]),
        get_usage=AsyncMock(),
        check_directory=AsyncMock(
            side_effect=lambda path: {"exists": True, "path": path}
        ),
        list_external_sessions=AsyncMock(return_value=[]),
        get_external_history=AsyncMock(),
        attach_external=AsyncMock(),
    )
    config = BridgeConfig(data_dir=str(tmp_path), default_adapter="pi_rpc")
    if platform == "slack":
        bridge = SlackBridge(
            bot_token="test_token",
            channel_id="C123",
            config=config,
            callbacks=callbacks,
        )
        context = {"user": "U123", "channel": "C123"}
        bridge._reply = AsyncMock()
    else:
        bridge = DiscordBridge(
            bot_token="test_token", channel_id=123, config=config, callbacks=callbacks
        )
        context = SimpleNamespace(
            channel=SimpleNamespace(id=123, send=AsyncMock()),
            author=SimpleNamespace(id=42),
        )
        bridge._restore_thread_mappings_from_store = lambda: None
    bridge.on_output = AsyncMock()

    await bridge._cmd_new(context, "")
    callbacks.create_session.assert_not_awaited()
    await bridge._cmd_new(context, "#1")

    callbacks.create_session.assert_awaited_once()
    assert callbacks.create_session.await_args.kwargs["directory"] == str(directory)
    callbacks.check_directory.assert_awaited_once_with(str(directory))
    bridge.on_output.assert_awaited_once_with(
        "sess_new", "Agent: Pi\nModel: provider/model"
    )
