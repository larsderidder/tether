"""Tether-local Slack bridge compatibility wrapper."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from agent_tether.slack.bot import SlackBridge as UpstreamSlackBridge
from agent_tether.thread_naming import adapter_to_runner

from tether.bridges.reaction_shortcuts import (
    ReactionShortcutError,
    parse_reaction_shortcut_message,
    reaction_matches,
)

logger = structlog.get_logger(__name__)


class SlackBridge(UpstreamSlackBridge):
    """Local Slack wrapper that adds reaction-driven session creation."""

    def __init__(
        self,
        bot_token: str,
        channel_id: str,
        slack_app_token: str | None = None,
        config=None,
        callbacks=None,
        get_session_directory=None,
        get_session_info=None,
        on_session_bound=None,
        *,
        reaction_new_session_enabled: bool = True,
        reaction_new_session_emoji: str = "✅",
    ) -> None:
        super().__init__(
            bot_token=bot_token,
            channel_id=channel_id,
            slack_app_token=slack_app_token,
            config=config,
            callbacks=callbacks,
            get_session_directory=get_session_directory,
            get_session_info=get_session_info,
            on_session_bound=on_session_bound,
        )
        self._reaction_new_session_enabled = reaction_new_session_enabled
        self._reaction_new_session_emoji = reaction_new_session_emoji or "✅"
        self._reaction_shortcuts_completed: set[str] = set()
        self._reaction_shortcuts_in_progress: set[str] = set()

    async def start(self) -> None:
        """Initialize Slack client and socket mode."""
        try:
            from slack_bolt.adapter.socket_mode.async_handler import (
                AsyncSocketModeHandler,
            )
            from slack_bolt.async_app import AsyncApp
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError:
            logger.error(
                "slack_sdk or slack_bolt not installed. Install with: pip install slack-sdk slack-bolt"
            )
            return

        self._client = AsyncWebClient(token=self._bot_token)

        app_token = self._slack_app_token
        if app_token:
            try:
                self._app = AsyncApp(token=self._bot_token)

                @self._app.event("message")
                async def handle_message(event: dict, say: Any) -> None:
                    await self._handle_message(event)

                @self._app.event("reaction_added")
                async def handle_reaction(event: dict, say: Any) -> None:
                    await self._handle_reaction_added(event)

                handler = AsyncSocketModeHandler(self._app, app_token)
                asyncio.create_task(handler.start_async())

                logger.info(
                    "Slack bridge initialized with socket mode",
                    channel_id=self._channel_id,
                    reaction_shortcuts=self._reaction_new_session_enabled,
                )
            except Exception:
                logger.exception(
                    "Failed to initialize Slack socket mode, falling back to basic mode"
                )
                logger.info(
                    "Slack bridge initialized (basic mode, no input forwarding)",
                    channel_id=self._channel_id,
                )
        else:
                logger.info(
                    "Slack bridge initialized (basic mode — set SLACK_APP_TOKEN for commands and input)",
                    channel_id=self._channel_id,
                )

    def _should_defer_new_message_to_reaction(self, text: str) -> bool:
        if not self._reaction_new_session_enabled:
            return False
        if "\n" not in text:
            return False
        try:
            return parse_reaction_shortcut_message(text) is not None
        except ReactionShortcutError:
            return True

    async def _handle_message(self, event: dict) -> None:
        """Route incoming Slack messages to commands or session input."""
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return

        text = event.get("text", "").strip()
        if not text:
            return

        thread_ts = event.get("thread_ts")
        ts = event.get("ts")

        if thread_ts:
            if text.startswith("!"):
                await self._dispatch_command(event, text)
                return
            session_id = self._session_for_thread(thread_ts)
            if not session_id:
                return
            await self._forward_input(event, session_id, text)
            return

        if text.startswith("!"):
            if (
                text.lower().startswith("!new")
                and self._should_defer_new_message_to_reaction(text)
                and ts
            ):
                return
            await self._dispatch_command(event, text)

    def _begin_reaction_shortcut(self, source_message_id: str) -> bool:
        if source_message_id in self._reaction_shortcuts_completed:
            return False
        if source_message_id in self._reaction_shortcuts_in_progress:
            return False
        self._reaction_shortcuts_in_progress.add(source_message_id)
        return True

    def _finish_reaction_shortcut(
        self, source_message_id: str, *, persist: bool
    ) -> None:
        self._reaction_shortcuts_in_progress.discard(source_message_id)
        if persist:
            self._reaction_shortcuts_completed.add(source_message_id)

    @staticmethod
    def _is_top_level_source_message(message: dict) -> bool:
        thread_ts = str(message.get("thread_ts") or "").strip()
        ts = str(message.get("ts") or "").strip()
        return not thread_ts or thread_ts == ts

    async def _handle_reaction_added(self, event: dict) -> None:
        """Create and start a new session from a reacted control-channel message."""
        if not self._reaction_new_session_enabled or not self._client:
            return

        item = event.get("item")
        if not isinstance(item, dict):
            return

        channel_id = str(item.get("channel") or "").strip()
        source_message_id = str(item.get("ts") or "").strip()
        if not channel_id or channel_id != self._channel_id:
            return
        if not source_message_id:
            return
        if not reaction_matches(
            self._reaction_new_session_emoji,
            str(event.get("reaction") or ""),
        ):
            return
        if not self._begin_reaction_shortcut(source_message_id):
            return

        persist = False
        reply_event = {"channel": channel_id, "ts": source_message_id}
        try:
            response = await self._client.conversations_history(
                channel=channel_id,
                latest=source_message_id,
                inclusive=True,
                limit=1,
            )
            if not response.get("ok"):
                raise RuntimeError("Slack could not load the reacted message")

            messages = response.get("messages") or []
            if not messages:
                return

            message = messages[0]
            if message.get("bot_id") or message.get("subtype") == "bot_message":
                return
            if not self._is_top_level_source_message(message):
                return

            shortcut = parse_reaction_shortcut_message(str(message.get("text") or ""))
            if shortcut is None:
                return

            adapter, directory = await self._parse_new_args(
                shortcut.args,
                base_session_id=None,
            )
            dir_short = directory.rstrip("/").rsplit("/", 1)[-1] or "Session"
            agent_label = (
                self._adapter_label(adapter)
                or self._adapter_label(self._config.default_adapter)
                or "Claude"
            )
            runner_type = adapter_to_runner(adapter or self._config.default_adapter)
            session_name = self._make_external_thread_name(
                directory=directory,
                session_id="",
                runner_type=runner_type,
            )

            session = await self._create_session_via_api(
                directory=directory,
                platform="slack",
                adapter=adapter,
                session_name=session_name,
            )
            session_id = str(session.get("id") or "").strip()
            if not session_id:
                raise RuntimeError("Tether did not return a session id")

            persist = True
            await self._send_input_or_start_via_api(
                session_id=session_id,
                text=shortcut.prompt,
            )
            await self._reply(
                reply_event,
                f"✅ New {agent_label} session created in {dir_short} from a checkmark reaction.",
            )
        except (ReactionShortcutError, ValueError) as exc:
            await self._reply(reply_event, str(exc))
        except Exception as exc:
            logger.exception(
                "Failed to create Slack session from reaction",
                source_message_id=source_message_id,
            )
            await self._reply(
                reply_event,
                f"Failed to create session from reaction: {exc}",
            )
        finally:
            self._finish_reaction_shortcut(source_message_id, persist=persist)
