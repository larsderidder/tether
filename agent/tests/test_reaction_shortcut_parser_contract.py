"""Contract tests for the planned checkmark-reaction shortcut."""

from unittest.mock import AsyncMock

import pytest

from agent_tether.base import BridgeCallbacks
from tether.bridges.reaction_shortcuts import (
    parse_reaction_shortcut_message,
    reaction_matches,
)


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


def _make_bridge(platform: str, **kwargs):
    if platform == "slack":
        from tether.bridges.slack.bot import SlackBridge

        return SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
            **kwargs,
        )
    if platform == "discord":
        from tether.bridges.discord.bot import DiscordBridge

        return DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
            **kwargs,
        )
    raise AssertionError(f"Unsupported platform: {platform}")


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["slack", "discord"])
async def test_top_level_reaction_contract_requires_directory_for_agent_only_message(
    platform: str,
) -> None:
    bridge = _make_bridge(platform, callbacks=_mock_callbacks())

    with pytest.raises(ValueError, match=r"Usage: !new <agent> <directory>"):
        await bridge._parse_new_args("codex", base_session_id=None)


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["slack", "discord"])
async def test_thread_seeded_reaction_contract_reuses_base_directory_for_agent_only_message(
    platform: str,
) -> None:
    bridge = _make_bridge(
        platform,
        callbacks=_mock_callbacks(),
        get_session_info=lambda _session_id: {
            "directory": "/worktrees/demo",
            "adapter": "claude_auto",
        },
    )

    adapter, directory = await bridge._parse_new_args(
        "codex",
        base_session_id="sess_existing",
    )

    assert adapter == "codex_sdk_sidecar"
    assert directory == "/worktrees/demo"


def test_parse_reaction_shortcut_message_extracts_args_and_prompt() -> None:
    shortcut = parse_reaction_shortcut_message(
        "!new codex /worktrees/tether\nFix the failing Discord tests."
    )

    assert shortcut is not None
    assert shortcut.args == "codex /worktrees/tether"
    assert shortcut.prompt == "Fix the failing Discord tests."


def test_parse_reaction_shortcut_message_accepts_plain_messages_when_enabled() -> None:
    shortcut = parse_reaction_shortcut_message(
        "LATEST THINKPAD CHECKMARK TEST 1",
        allow_plain_message=True,
    )

    assert shortcut is not None
    assert shortcut.args is None
    assert shortcut.prompt == "LATEST THINKPAD CHECKMARK TEST 1"


def test_parse_reaction_shortcut_message_ignores_plain_messages_when_disabled() -> None:
    shortcut = parse_reaction_shortcut_message("LATEST THINKPAD CHECKMARK TEST 1")

    assert shortcut is None


def test_parse_reaction_shortcut_message_does_not_treat_other_commands_as_plain() -> None:
    shortcut = parse_reaction_shortcut_message(
        "!help",
        allow_plain_message=True,
    )

    assert shortcut is None


def test_reaction_matches_accepts_slack_and_discord_checkmark_forms() -> None:
    assert reaction_matches("✅", "white_check_mark") is True
    assert reaction_matches("white_check_mark", "✅") is True
    assert reaction_matches("✅", "eyes") is False
