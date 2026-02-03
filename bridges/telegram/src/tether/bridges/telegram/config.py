"""Configuration loading from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    """Bridge configuration loaded from environment."""

    telegram_bot_token: str
    telegram_forum_group_id: int
    state_file: str
    agent_url: str
    agent_token: str


def load_config() -> Config:
    """Load configuration from environment variables.

    Required:
        TELEGRAM_BOT_TOKEN: Bot token from @BotFather
        TELEGRAM_FORUM_GROUP_ID: Supergroup ID with topics enabled

    Optional:
        STATE_FILE: Path for persistent state (default: ./tether_telegram_state.json)
        AGENT_URL: Agent base URL (default: http://localhost:8787)
        AGENT_TOKEN: Agent auth token (default: empty)
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")

    forum_group_id_str = os.environ.get("TELEGRAM_FORUM_GROUP_ID")
    if not forum_group_id_str:
        raise ValueError("TELEGRAM_FORUM_GROUP_ID is required")
    forum_group_id = int(forum_group_id_str)

    state_file = os.environ.get("STATE_FILE", "./tether_telegram_state.json")
    agent_url = os.environ.get("AGENT_URL", "http://localhost:8787")
    agent_token = os.environ.get("AGENT_TOKEN", "")

    return Config(
        telegram_bot_token=token,
        telegram_forum_group_id=forum_group_id,
        state_file=state_file,
        agent_url=agent_url.rstrip("/"),
        agent_token=agent_token,
    )
