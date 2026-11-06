"""FastAPI application entrypoint for the agent server."""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
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
from tether.api.runner_events import get_runner_registry
from tether.api.runner_registry import RUNNER_SHUTDOWN_TIMEOUT_SECONDS
from tether.middleware import (
    http_exception_handler,
    request_logging_middleware,
    validation_exception_handler,
)
from tether.external_session_watcher import external_session_watcher
from tether.log_config import configure_logging
from tether.maintenance import (
    maintenance_loop,
    reconcile_persisted_lifecycle_states,
    telegram_topic_cleanup_loop,
)
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
SHUTDOWN_PHASE_TIMEOUT_SECONDS = 5.0


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
    await reconcile_persisted_lifecycle_states()
    await external_session_watcher.start()
    maintenance_task = asyncio.create_task(maintenance_loop())
    topic_cleanup_task = asyncio.create_task(telegram_topic_cleanup_loop())
    log_ui_urls(port=settings.port())
    try:
        yield
    finally:
        maintenance_task.cancel()
        topic_cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await topic_cleanup_task
        with suppress(asyncio.CancelledError):
            await maintenance_task
        try:
            await _shutdown_services()
        except Exception:
            logger.exception("Shutdown incomplete")
        else:
            logger.info("Shutdown complete")


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
                reaction_new_session_enabled=settings.bridge_reaction_new_session_enabled(),
                reaction_new_session_emoji=settings.bridge_reaction_new_session_emoji(),
                reaction_new_session_allow_plain_messages=settings.bridge_reaction_new_session_allow_plain_messages(),
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
                    auto_pair_user_ids=settings.discord_auto_pair_user_ids(),
                    pairing_code=settings.discord_pairing_code(),
                    guild_id=settings.discord_guild_id(),
                    reaction_new_session_enabled=settings.bridge_reaction_new_session_enabled(),
                    reaction_new_session_emoji=settings.bridge_reaction_new_session_emoji(),
                    reaction_new_session_allow_plain_messages=settings.bridge_reaction_new_session_allow_plain_messages(),
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
                guild_id = settings.discord_guild_id()
                if guild_id:
                    logger.info(
                        "Discord bridge started. The control channel will be auto-created or reused after the bot connects.",
                        guild_id=guild_id,
                        channel_name=bridge._control_channel_name,
                    )
                else:
                    pairing_code = (
                        settings.discord_pairing_code() or bridge._pairing_code
                    )
                    logger.info(
                        "Discord bridge started. Run !setup in your Discord channel to configure it.",
                        code=pairing_code,
                    )
            else:
                logger.info("Discord bridge registered and started")
        except Exception:
            logger.exception("Failed to initialize Discord bridge")


async def _shutdown_services() -> None:
    """Stop watchers, runners, bridges, and managed sidecars in flush-safe order."""
    failures: list[str] = []

    try:
        await asyncio.wait_for(
            external_session_watcher.stop(), timeout=SHUTDOWN_PHASE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        logger.exception("External session watcher shutdown timed out")
        failures.append("external session watcher")
    except asyncio.CancelledError:
        logger.warning("External session watcher shutdown cancelled")
        failures.append("external session watcher")
    except Exception:
        logger.exception("External session watcher shutdown failed")
        failures.append("external session watcher")

    try:
        await asyncio.wait_for(
            get_runner_registry().shutdown(),
            timeout=RUNNER_SHUTDOWN_TIMEOUT_SECONDS + 1.0,
        )
    except asyncio.TimeoutError:
        logger.exception("Runner shutdown timed out")
        failures.append("runners")
    except asyncio.CancelledError:
        logger.warning("Runner shutdown cancelled")
        failures.append("runners")
    except Exception:
        logger.exception("Runner shutdown failed")
        failures.append("runners")

    try:
        await _stop_bridges()
    except Exception:
        logger.exception("Bridge shutdown failed")
        failures.append("bridges")
    finally:
        if settings.opencode_sidecar_managed():
            from tether.runner.opencode_sidecar_manager import (
                stop_managed_opencode_sidecar,
            )

            try:
                await asyncio.wait_for(
                    stop_managed_opencode_sidecar(),
                    timeout=SHUTDOWN_PHASE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.exception("Managed OpenCode sidecar shutdown timed out")
                failures.append("managed OpenCode sidecar")
            except asyncio.CancelledError:
                logger.warning("Managed OpenCode sidecar shutdown cancelled")
                failures.append("managed OpenCode sidecar")
            except Exception:
                logger.exception("Managed OpenCode sidecar shutdown failed")
                failures.append("managed OpenCode sidecar")

    if failures:
        raise RuntimeError("Shutdown incomplete: " + ", ".join(failures))


async def _stop_bridges() -> None:
    """Stop all registered messaging bridges."""

    async def stop_bridge(platform: str, bridge: object) -> str | None:
        stop = getattr(bridge, "stop", None)
        if not callable(stop):
            return None
        try:
            await asyncio.wait_for(stop(), timeout=SHUTDOWN_PHASE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.exception("Bridge shutdown timed out", platform=platform)
            return platform
        except asyncio.CancelledError:
            logger.warning("Bridge shutdown cancelled", platform=platform)
            return platform
        except Exception:
            logger.exception("Failed to stop bridge", platform=platform)
            return platform
        return None

    bridges = [
        (platform, bridge)
        for platform in bridge_manager.list_bridges()
        if (bridge := bridge_manager.get_bridge(platform)) is not None
    ]
    failures = [
        platform
        for platform in await asyncio.gather(
            *(stop_bridge(platform, bridge) for platform, bridge in bridges)
        )
        if platform is not None
    ]
    if failures:
        names = ", ".join(sorted(failures))
        raise RuntimeError(f"Bridge shutdown failed for: {names}")


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
    """Install signal handlers that delegate graceful shutdown to Uvicorn."""
    global _interrupted

    def _handler(signum, frame):
        global _interrupted
        if _interrupted:
            logger.warning("Repeated interrupt during shutdown")
        else:
            _interrupted = True
            logger.info("Shutting down")
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
                    hint="Stop the other process or use: tether start --port <other>",
                )
                sys.exit(1)
        raise


if __name__ == "__main__":
    run()
else:
    app.state.agent_token = settings.token()
