"""Interactive setup wizard for Tether.

Run via ``tether init`` to generate ``~/.config/tether/config.env``.
"""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path

from tether.config import config_dir


def run_wizard() -> None:
    """Run the interactive init wizard."""
    print()
    print("Welcome to Tether!")
    print("This wizard will create your configuration file.")
    print()

    config: dict[str, str] = {}

    # 1. Auth token
    token = secrets.token_urlsafe(32)
    config["TETHER_AGENT_TOKEN"] = token
    print(f"Generated auth token: {token[:8]}...")

    # 2. Adapter detection
    adapter = _detect_adapter()
    if adapter:
        config["TETHER_AGENT_ADAPTER"] = adapter

    # 3. Bridge setup
    _configure_bridge(config)

    # 4. Write config
    dest = config_dir() / "config.env"
    _write_config(config, dest)

    print()
    print(f"Configuration written to {dest}")
    print()
    print("Next steps:")
    print(f"  tether start")
    print()
    print(f"Your auth token (save this for connecting from your browser):")
    print(f"  {token}")
    print()


def _detect_adapter() -> str | None:
    """Detect available AI CLI tools and suggest an adapter."""
    has_claude = _detect_claude_cli()
    if has_claude:
        print("Detected `claude` CLI on PATH — using claude_auto adapter.")
        return "claude_auto"

    print("No `claude` CLI detected. Defaulting to claude_api adapter.")
    print("You will need to set ANTHROPIC_API_KEY in your config.")
    return "claude_api"


def _detect_claude_cli() -> bool:
    """Check if the `claude` CLI is available on PATH."""
    return shutil.which("claude") is not None


def _configure_bridge(config: dict[str, str]) -> None:
    """Ask about messaging bridge setup."""
    print()
    choice = _prompt_choice(
        "Set up a messaging bridge?",
        ["Telegram", "Slack", "Discord", "Skip"],
    )

    if choice == "Skip":
        return

    if choice == "Telegram":
        print()
        print("Create a Telegram bot via @BotFather and enable topics in your group.")
        config["TELEGRAM_BOT_TOKEN"] = _prompt("Telegram bot token")
        config["TELEGRAM_FORUM_GROUP_ID"] = _prompt("Telegram forum group ID")

    elif choice == "Slack":
        print()
        print("Create a Slack app at https://api.slack.com/apps with Socket Mode enabled.")
        config["SLACK_BOT_TOKEN"] = _prompt("Slack bot token (xoxb-...)")
        config["SLACK_APP_TOKEN"] = _prompt("Slack app token (xapp-...)")
        config["SLACK_CHANNEL_ID"] = _prompt("Slack channel ID")

    elif choice == "Discord":
        print()
        print("Create a Discord bot at https://discord.com/developers/applications.")
        config["DISCORD_BOT_TOKEN"] = _prompt("Discord bot token")
        channel = _prompt("Discord channel ID (optional, press Enter to skip)")
        if channel:
            config["DISCORD_CHANNEL_ID"] = channel


def _write_config(config: dict[str, str], path: Path) -> None:
    """Write config dict to an env file."""
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for key, value in config.items():
        # Quote values that contain spaces or special characters
        if " " in value or "#" in value:
            lines.append(f'{key}="{value}"')
        else:
            lines.append(f"{key}={value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Restrict permissions — config contains secrets
    os.chmod(path, 0o600)


def _prompt(label: str) -> str:
    """Prompt for a single value."""
    return input(f"  {label}: ").strip()


def _prompt_choice(question: str, options: list[str]) -> str:
    """Present a numbered choice list and return the selected option."""
    print(question)
    for i, opt in enumerate(options, 1):
        print(f"  {i}) {opt}")

    while True:
        raw = input(f"  Choice [1-{len(options)}]: ").strip()
        try:
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1]
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(options)}.")
