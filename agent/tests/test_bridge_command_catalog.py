"""
Tests for shared bridge command metadata.
"""

from tether.bridges.command_catalog import help_text, telegram_menu_commands


def test_telegram_menu_contains_local_commands() -> None:
    """
    Telegram menu metadata includes local wrapper commands.
    """
    command_names = [name for name, _ in telegram_menu_commands()]

    assert "sync" in command_names
    assert "compact" in command_names
    assert "diff" in command_names
    assert "log" in command_names


def test_bridge_help_does_not_claim_chat_commit_or_push() -> None:
    """
    Bridge help only lists chat commands that are implemented.
    """
    for platform, prefix in (("telegram", "/"), ("slack", "!"), ("discord", "!")):
        text = help_text(platform, prefix=prefix)

        assert f"{prefix}sync" in text
        assert f"{prefix}commit" not in text
        assert f"{prefix}push" not in text
