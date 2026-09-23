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
@pytest.mark.parametrize(
    "parent_adapter", [None, "claude_auto", "codex_sdk_sidecar", "pi_rpc"]
)
async def test_bare_new_and_numbered_choice_preserve_path_with_spaces(
    platform: str, tmp_path: Path, parent_adapter: str | None, monkeypatch
) -> None:
    """Default-agent choices create exactly the displayed directory and announce configuration."""
    directory = tmp_path / "project with spaces"
    directory.mkdir()
    monkeypatch.setenv("TETHER_DEFAULT_AGENT_ADAPTER", "pi")
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
    config = BridgeConfig(data_dir=str(tmp_path))
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
    if parent_adapter:
        other = tmp_path / "other"
        other.mkdir()
        callbacks.list_sessions.return_value = [{"directory": str(other)}]
        bridge._get_session_info = lambda _: {
            "directory": str(directory),
            "adapter": parent_adapter,
            "model": "parent-model",
        }
        if platform == "slack":
            context["thread_ts"] = "111.222"
            bridge._thread_ts["sess_parent"] = "111.222"
        else:
            bridge._thread_ids["sess_parent"] = 123

    await bridge._cmd_new(context, "")
    callbacks.create_session.assert_not_awaited()
    choices = bridge._recent_new_directories[bridge._recent_choice_key(context)]
    assert choices[0] == str(directory)
    await bridge._cmd_new(context, "#1")

    callbacks.create_session.assert_awaited_once()
    assert callbacks.create_session.await_args.kwargs["directory"] == str(directory)
    assert callbacks.create_session.await_args.kwargs["adapter"] == "pi_rpc"
    assert callbacks.create_session.await_args.kwargs.get("model") is None
    callbacks.check_directory.assert_awaited_once_with(str(directory))
    bridge.on_output.assert_awaited_once_with(
        "sess_new", "Agent: Pi\nModel: provider/model"
    )
