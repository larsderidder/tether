"""FastAPI application entrypoint for the agent server."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
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
    if settings.adapter() == "opencode" and settings.opencode_sidecar_managed():
        from tether.runner.opencode_sidecar_manager import (
            ensure_opencode_sidecar_started,
        )

        await ensure_opencode_sidecar_started()
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
        # Give bridges and sidecars a few seconds to stop; don't block
        # shutdown indefinitely if something hangs.
        try:
            await asyncio.wait_for(_shutdown_services(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Shutdown timed out after 5s, forcing exit")
        logger.info("Shutdown complete")
        # Force exit: blocking threads (SSE readers, subprocess pipes) can
        # keep the process alive indefinitely after uvicorn stops serving.
        os._exit(0)


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
            from tether.bridges.telegram.bot import TelegramBridge

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
            from tether.bridges.slack.bot import SlackBridge

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
            from tether.bridges.discord.bot import DiscordBridge, DiscordConfig

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
            if not discord_channel:
                pairing_code = settings.discord_pairing_code() or bridge._pairing_code
                logger.info(
                    "Discord bridge started. Run !setup in your Discord channel to configure it.",
                    code=pairing_code,
                )
            else:
                logger.info("Discord bridge registered and started")
        except Exception:
            logger.exception("Failed to initialize Discord bridge")


async def _shutdown_services() -> None:
    """Stop bridges and managed sidecars."""
    await _stop_bridges()
    if settings.opencode_sidecar_managed():
        from tether.runner.opencode_sidecar_manager import (
            stop_managed_opencode_sidecar,
        )

        await stop_managed_opencode_sidecar()


async def _stop_bridges() -> None:
    """Stop all registered messaging bridges."""
    for platform in bridge_manager.list_bridges():
        bridge = bridge_manager.get_bridge(platform)
        if bridge and hasattr(bridge, "stop"):
            try:
                await bridge.stop()
            except Exception:
                logger.exception("Failed to stop bridge", platform=platform)


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


_interrupted = False


def _install_signal_handlers() -> None:
    """Install signal handlers that force-exit on repeated ctrl+c.

    Uvicorn handles the first SIGINT to start graceful shutdown, but
    blocking threads (SSE readers, subprocess pipes) can prevent the
    process from actually exiting. On the second SIGINT we force-kill.
    """
    global _interrupted

    def _handler(signum, frame):
        global _interrupted
        if _interrupted:
            logger.info("Forced exit (repeated interrupt)")
            os._exit(1)
        _interrupted = True
        logger.info("Shutting down (press ctrl+c again to force)")
        # Re-raise so uvicorn's handler fires too.
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def run() -> None:
    """Entry point for ``tether start``."""
    _install_signal_handlers()
    port = settings.port()
    app.state.agent_token = settings.token()
    try:
        uvicorn.run(
            "tether.main:app",
            host=settings.host(),
            port=port,
            reload=False,
        )
    except SystemExit:
        # uvicorn calls sys.exit(1) on bind failure; check if port is in use.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                logger.error(
                    "Port already in use. Is Tether already running?",
                    port=port,
                    hint=f"Stop the other process or use: tether start --port <other>",
                )
                sys.exit(1)
        raise


if __name__ == "__main__":
    run()
else:
    app.state.agent_token = settings.token()
