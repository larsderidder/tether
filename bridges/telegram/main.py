"""Entry point for the Telegram bridge."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .config import load_config
from .sse_client import AgentClient
from .telegram_bot import TelegramBridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    """Run the Telegram bridge."""
    try:
        config = load_config()
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)

    agent = AgentClient(config.agent_url, config.agent_token)
    bridge = TelegramBridge(config, agent)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def shutdown_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)

    logger.info("Starting Telegram bridge...")
    logger.info("Agent URL: %s", config.agent_url)
    logger.info("Chat ID: %s", config.telegram_chat_id)

    await agent.start()
    try:
        await bridge.start()
        logger.info("Telegram bridge running. Press Ctrl+C to stop.")
        await stop_event.wait()
    finally:
        logger.info("Stopping Telegram bridge...")
        await bridge.stop()
        await agent.stop()
        logger.info("Telegram bridge stopped.")


def run() -> None:
    """Entry point for running the bridge."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
