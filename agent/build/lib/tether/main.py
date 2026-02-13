"""FastAPI application entrypoint for the agent server."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

# Ensure local `.env` (cwd) and `~/.config/tether/config.env` are applied even when
# running `python agent/tether/main.py` directly (e.g. via PyCharm).
#
# Safe to call multiple times: it never overwrites already-set env vars.
from tether.config import load_config

load_config()

from tether.api import api_router, root_router
from tether.middleware import (
    http_exception_handler,
    request_logging_middleware,
    validation_exception_handler,
)
from tether.log_config import configure_logging
from tether.maintenance import maintenance_loop
from tether.settings import settings
from tether.startup import log_ui_urls
from tether.bridges.glue import (
    bridge_manager,
    make_bridge_callbacks,
    make_bridge_config,
    get_session_directory,
    get_session_info,
    on_session_bound,
    get_sessions_for_restore,
)

configure_logging()
logger = structlog.get_logger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.agent_token = settings.token()
    await _init_bridges()
    _subscribe_existing_sessions()
    maintenance_task = asyncio.create_task(maintenance_loop())
    log_ui_urls(port=settings.port())
    try:
        yield
    finally:
        maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance_task


app = FastAPI(lifespan=lifespan)

app.middleware("http")(request_logging_middleware)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)


async def _init_bridges() -> None:
    """Initialize and register messaging platform bridges based on env vars."""
    config = make_bridge_config()
    callbacks = make_bridge_callbacks()
    sessions = get_sessions_for_restore()

    # Telegram bridge
    telegram_token = settings.telegram_bot_token()
    telegram_group_id = settings.telegram_group_id()
    if telegram_token and telegram_group_id:
        try:
            from agent_tether import TelegramBridge

            bridge = TelegramBridge(
                bot_token=telegram_token,
                forum_group_id=telegram_group_id,
                config=config,
                callbacks=callbacks,
                get_session_directory=get_session_directory,
                get_session_info=get_session_info,
                on_session_bound=on_session_bound,
            )
            await bridge.start()
            bridge_manager.register_bridge("telegram", bridge)
            logger.info("Telegram bridge registered and started")
        except Exception:
            logger.exception("Failed to initialize Telegram bridge")

    # Slack bridge
    slack_token = settings.slack_bot_token()
    slack_channel = settings.slack_channel_id()
    if slack_token and slack_channel:
        try:
            from agent_tether import SlackBridge

            bridge = SlackBridge(
                bot_token=slack_token,
                channel_id=slack_channel,
                slack_app_token=settings.slack_app_token(),
                config=config,
                callbacks=callbacks,
                get_session_directory=get_session_directory,
                get_session_info=get_session_info,
                on_session_bound=on_session_bound,
            )
            await bridge.start()
            bridge.restore_thread_mappings(sessions)
            bridge_manager.register_bridge("slack", bridge)
            logger.info("Slack bridge registered and started")
        except Exception:
            logger.exception("Failed to initialize Slack bridge")

    # Discord bridge
    discord_token = settings.discord_bot_token()
    discord_channel = settings.discord_channel_id()
    if discord_token:
        try:
            from agent_tether import DiscordBridge
            from agent_tether.discord.bot import DiscordConfig

            bridge = DiscordBridge(
                bot_token=discord_token,
                channel_id=discord_channel,
                discord_config=DiscordConfig(
                    require_pairing=settings.discord_require_pairing(),
                    allowed_user_ids=settings.discord_allowed_user_ids(),
                    pairing_code=settings.discord_pairing_code(),
                ),
                config=config,
                callbacks=callbacks,
                get_session_directory=get_session_directory,
                get_session_info=get_session_info,
                on_session_bound=on_session_bound,
            )
            await bridge.start()
            bridge.restore_thread_mappings(sessions)
            bridge_manager.register_bridge("discord", bridge)
            logger.info("Discord bridge registered and started")
        except Exception:
            logger.exception("Failed to initialize Discord bridge")


def _subscribe_existing_sessions() -> None:
    """Subscribe bridge events for sessions that have platform bindings.

    Called on startup to handle server restarts — any sessions that were
    previously bound to a platform get their bridge subscriber reattached.
    """
    from tether.bridges.glue import bridge_subscriber
    from tether.store import store

    for session in store.list_sessions():
        if session.platform:
            bridge_subscriber.subscribe(session.id, session.platform)
            logger.info(
                "Resubscribed bridge for session",
                session_id=session.id,
                platform=session.platform,
            )


app.include_router(api_router)
app.include_router(root_router)

def run() -> None:
    """Entry point for ``tether start``."""
    app.state.agent_token = settings.token()
    uvicorn.run(
        "tether.main:app",
        host=settings.host(),
        port=settings.port(),
        reload=False,
    )


if __name__ == "__main__":
    run()
else:
    app.state.agent_token = settings.token()
