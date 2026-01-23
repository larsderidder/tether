"""Configuration loading from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    """Bridge configuration loaded from environment."""

    telegram_bot_token: str
    telegram_chat_id: int
    agent_url: str
    agent_token: str


def load_config() -> Config:
    """Load configuration from environment variables.

    Required:
        TELEGRAM_BOT_TOKEN: Bot token from @BotFather
        TELEGRAM_CHAT_ID: Chat ID to send/receive messages

    Optional:
        AGENT_URL: Agent base URL (default: http://localhost:8787)
        AGENT_TOKEN: Agent auth token (default: empty)
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")

    chat_id_str = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id_str:
        raise ValueError("TELEGRAM_CHAT_ID is required")
    chat_id = int(chat_id_str)

    agent_url = os.environ.get("AGENT_URL", "http://localhost:8787")
    agent_token = os.environ.get("AGENT_TOKEN", "")

    return Config(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        agent_url=agent_url.rstrip("/"),
        agent_token=agent_token,
    )
