"""Tether-local Slack bridge compatibility wrapper."""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

import structlog
from agent_tether.slack.bot import SlackBridge as UpstreamSlackBridge
from agent_tether.thread_naming import adapter_to_runner

from tether.bridges.debug_attachments import build_error_debug_bundle
from tether.bridges.rich_output import render_slack_messages
from tether.bridges.reaction_shortcuts import (
    ReactionShortcutError,
    parse_reaction_shortcut_message,
    reaction_matches,
)
from tether.output_postprocess import PublishedAttachment
from tether.settings import settings

logger = structlog.get_logger(__name__)

_ERROR_ATTACHMENT_DELAY_S = 0.35


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
        reaction_new_session_allow_plain_messages: bool = False,
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
        self._reaction_new_session_allow_plain_messages = (
            reaction_new_session_allow_plain_messages
        )
        self._reaction_shortcuts_completed: set[str] = set()
        self._reaction_shortcuts_in_progress: set[str] = set()
        self._pending_error_attachment_tasks: dict[str, asyncio.Task] = {}

    async def on_output(
        self, session_id: str, text: str, metadata: dict | None = None
    ) -> None:
        if not self._client:
            logger.warning("Slack client not initialized")
            return

        thread_ts = self._thread_ts.get(session_id)
        if not thread_ts:
            logger.warning("No Slack thread for session", session_id=session_id)
            return

        try:
            for message in render_slack_messages(text) or [text]:
                await self._client.chat_postMessage(
                    channel=self._channel_id,
                    thread_ts=thread_ts,
                    text=message,
                )
        except Exception:
            logger.exception("Failed to send Slack message", session_id=session_id)

        await self._send_requested_output_attachments(session_id, metadata=metadata)

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

    def _prepare_thread_name_update(
        self, session_id: str, session_name: str
    ) -> tuple[str, str]:
        current_name = self._thread_names.get(session_id, "")
        desired_name = " ".join((session_name or "").split()) or "Session"
        if current_name == desired_name:
            return current_name, desired_name

        if current_name:
            self._release_thread_name(session_id)
        resolved_name = self._pick_unique_thread_name(desired_name)
        self._reserve_thread_name(session_id, resolved_name)
        return current_name, resolved_name

    async def rename_thread(self, session_id: str, session_name: str) -> str:
        """Rename a Slack thread by updating its parent message."""
        if not self._client:
            raise RuntimeError("Slack client not initialized")

        thread_ts = self._thread_ts.get(session_id)
        if not thread_ts:
            raise RuntimeError(f"No Slack thread mapping for session {session_id}")

        previous_name, resolved_name = self._prepare_thread_name_update(
            session_id, session_name
        )
        try:
            await self._client.chat_update(
                channel=self._channel_id,
                ts=thread_ts,
                text=f"*Session:* {resolved_name}",
            )
        except Exception as exc:
            if self._thread_names.get(session_id) == resolved_name:
                self._release_thread_name(session_id)
            if previous_name:
                self._reserve_thread_name(session_id, previous_name)
            raise RuntimeError(f"Failed to rename Slack thread: {exc}") from exc

        logger.info(
            "Renamed Slack thread",
            session_id=session_id,
            thread_ts=thread_ts,
            name=resolved_name,
        )
        return resolved_name

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

    async def _cmd_sync(self, event: dict) -> None:
        """Handle !sync and !sync force for attached external sessions."""
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            await self._reply(event, "Use this command inside a session thread.")
            return

        session_id = self._session_for_thread(thread_ts)
        if not session_id:
            await self._reply(event, "No session linked to this thread.")
            return

        if not self._callbacks.sync_session:
            await self._reply(event, "Sync is not supported by this Tether version.")
            return

        text = str(event.get("text") or "").strip()
        force = text.lower().split()[:2] == ["!sync", "force"]

        try:
            result = await self._callbacks.sync_session(session_id, force=force)
            synced = result.get("synced", 0)
            total = result.get("total", 0)
            if synced:
                prefix = "🔄 Force-synced" if force else "🔄 Synced"
                await self._reply(event, f"{prefix} {synced} message(s) ({total} total).")
            else:
                await self._reply(event, f"✅ Already up to date ({total} message(s) total).")
        except TypeError:
            result = await self._callbacks.sync_session(session_id)
            synced = result.get("synced", 0)
            total = result.get("total", 0)
            if synced:
                await self._reply(event, f"🔄 Synced {synced} new message(s) ({total} total).")
            else:
                await self._reply(event, f"✅ Already up to date ({total} message(s) total).")
        except Exception as exc:
            logger.exception("Failed to sync session")
            await self._reply(event, f"Failed to sync: {exc}")

    async def _cancel_pending_error_attachment_task(self, session_id: str) -> None:
        task = self._pending_error_attachment_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _send_requested_output_attachments(
        self,
        session_id: str,
        *,
        metadata: dict | None = None,
    ) -> None:
        if not self._client:
            return

        attachments = [
            attachment
            for attachment in (
                PublishedAttachment.from_metadata(item)
                for item in (metadata or {}).get("attachments") or []
            )
            if attachment is not None
        ]
        if not attachments:
            return

        thread_ts = self._thread_ts.get(session_id)
        if not thread_ts:
            return

        failures: list[str] = []
        for attachment in attachments:
            try:
                await self._client.files_upload_v2(
                    channel=self._channel_id,
                    thread_ts=thread_ts,
                    file=attachment.path,
                    filename=attachment.filename,
                    title=attachment.title or attachment.filename,
                )
            except Exception:
                logger.exception(
                    "Failed to upload Slack output attachment",
                    session_id=session_id,
                    attachment_path=attachment.path,
                )
                failures.append(attachment.filename)

        if failures:
            try:
                await self._client.chat_postMessage(
                    channel=self._channel_id,
                    thread_ts=thread_ts,
                    text="Attachment upload failed: " + ", ".join(failures),
                )
            except Exception:
                logger.exception(
                    "Failed to send Slack attachment failure notice",
                    session_id=session_id,
                )

    async def _send_error_attachment_bundle(
        self,
        session_id: str,
        metadata: dict | None = None,
    ) -> bool:
        if not self._client:
            return False

        thread_ts = self._thread_ts.get(session_id)
        if not thread_ts:
            return False

        if not self._should_send_error_status(session_id):
            return True

        bundle = build_error_debug_bundle(session_id, metadata=metadata)
        try:
            first = True
            for attachment in bundle.attachments:
                kwargs = {
                    "channel": self._channel_id,
                    "thread_ts": thread_ts,
                    "filename": attachment.filename,
                    "title": attachment.title or attachment.filename,
                    "content": attachment.content,
                    "snippet_type": "text",
                }
                if first:
                    kwargs["initial_comment"] = bundle.message
                await self._client.files_upload_v2(**kwargs)
                first = False

            if first:
                await self._client.chat_postMessage(
                    channel=self._channel_id,
                    thread_ts=thread_ts,
                    text=bundle.message,
                )
        except Exception:
            logger.exception(
                "Failed to send Slack error attachment bundle",
                session_id=session_id,
            )
            try:
                await self._client.chat_postMessage(
                    channel=self._channel_id,
                    thread_ts=thread_ts,
                    text=":x: Status: error",
                )
            except Exception:
                logger.exception(
                    "Failed to send Slack fallback error status",
                    session_id=session_id,
                )
        return True

    def _schedule_error_attachment_bundle(
        self,
        session_id: str,
        metadata: dict | None = None,
    ) -> None:
        existing = self._pending_error_attachment_tasks.pop(session_id, None)
        if existing and not existing.done():
            existing.cancel()

        async def _delayed_send() -> None:
            try:
                await asyncio.sleep(_ERROR_ATTACHMENT_DELAY_S)
                handled = await self._send_error_attachment_bundle(
                    session_id,
                    metadata=metadata,
                )
                if not handled:
                    await super(SlackBridge, self).on_status_change(
                        session_id,
                        "error",
                        metadata=metadata,
                    )
            except asyncio.CancelledError:
                return
            finally:
                self._pending_error_attachment_tasks.pop(session_id, None)

        self._pending_error_attachment_tasks[session_id] = asyncio.create_task(
            _delayed_send()
        )

    async def on_status_change(
        self, session_id: str, status: str, metadata: dict | None = None
    ) -> None:
        if status != "error" or not settings.debug_attach_logs():
            await self._cancel_pending_error_attachment_task(session_id)
            await super().on_status_change(session_id, status, metadata=metadata)
            return

        message = str((metadata or {}).get("message") or "").strip()
        if message:
            await self._cancel_pending_error_attachment_task(session_id)
            handled = await self._send_error_attachment_bundle(
                session_id,
                metadata=metadata,
            )
            if not handled:
                await super().on_status_change(session_id, status, metadata=metadata)
            return

        self._schedule_error_attachment_bundle(session_id, metadata=metadata)

    async def on_session_removed(self, session_id: str) -> None:
        await self._cancel_pending_error_attachment_task(session_id)
        await super().on_session_removed(session_id)

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

    async def _resolve_reaction_shortcut_target(
        self, shortcut
    ) -> tuple[str | None, str]:
        if shortcut.args is not None:
            return await self._parse_new_args(shortcut.args, base_session_id=None)
        directory = await self._resolve_directory_arg(
            os.getcwd(),
            base_directory=None,
        )
        return self._config.default_adapter, directory

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

            shortcut = parse_reaction_shortcut_message(
                str(message.get("text") or ""),
                allow_plain_message=self._reaction_new_session_allow_plain_messages,
            )
            if shortcut is None:
                return

            adapter, directory = await self._resolve_reaction_shortcut_target(shortcut)
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
