"""Tether-local Discord bridge compatibility wrapper.

Keep the upstream ``agent_tether`` bridge as the source of truth for Discord
behavior, but override thread creation for text channels so new session threads
are public and discoverable in the configured control channel.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import io
import os
import re
import socket
from typing import Any

import structlog
from agent_tether.discord.bot import DiscordBridge as UpstreamDiscordBridge
from agent_tether.discord.bot import DiscordConfig as UpstreamDiscordConfig
from agent_tether.discord.pairing_state import save as save_pairing_state
from agent_tether.thread_naming import adapter_to_runner

from tether.bridges.debug_attachments import build_error_debug_bundle
from tether.bridges.reaction_shortcuts import (
    ReactionShortcutError,
    parse_reaction_shortcut_message,
    reaction_matches,
)
from tether.output_postprocess import PublishedAttachment
from tether.settings import settings

logger = structlog.get_logger(__name__)

_DISCORD_THREAD_NAME_LIMIT = 100
_DISCORD_STARTER_TEXT_LIMIT = 2000
_DISCORD_AUTO_ARCHIVE_MINUTES = 1440
_ERROR_ATTACHMENT_DELAY_S = 0.35


def _hostname_slug() -> str:
    hostname = socket.gethostname().split(".", 1)[0].strip().lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", hostname).strip("-")
    return slug or "tether"


@dataclass
class DiscordConfig:
    """Tether-local Discord config compatibility shim."""

    require_pairing: bool = False
    allowed_user_ids: list[int] | None = None
    auto_pair_user_ids: list[int] | None = None
    pairing_code: str | None = None
    guild_id: int = 0
    reaction_new_session_enabled: bool = True
    reaction_new_session_emoji: str = "✅"
    reaction_new_session_allow_plain_messages: bool = False


class DiscordBridge(UpstreamDiscordBridge):
    """Compatibility wrapper for the upstream Discord bridge.

    Upstream ``channel.create_thread(...)`` creates private threads when the
    configured control channel is a regular Discord text channel. Those private
    threads are effectively invisible in the machine channel, which makes the
    Discord surface look empty. Tether needs visible public threads there.

    For text channels, create a starter message and open the thread from that
    message so Discord treats it as a public thread. For any other channel type,
    fall back to upstream behavior unchanged.
    """

    def __init__(
        self,
        bot_token: str,
        channel_id: int,
        discord_config: DiscordConfig | UpstreamDiscordConfig | None = None,
        **kwargs: Any,
    ) -> None:
        local_config = discord_config or DiscordConfig()
        upstream_config = UpstreamDiscordConfig(
            require_pairing=getattr(local_config, "require_pairing", False),
            allowed_user_ids=getattr(local_config, "allowed_user_ids", None),
            pairing_code=getattr(local_config, "pairing_code", None),
        )
        super().__init__(
            bot_token=bot_token,
            channel_id=channel_id,
            discord_config=upstream_config,
            **kwargs,
        )
        raw_auto_pair_ids = getattr(local_config, "auto_pair_user_ids", None) or []
        auto_pair_user_ids: set[int] = set()
        for user_id in raw_auto_pair_ids:
            raw_user_id = str(user_id).strip()
            if not raw_user_id:
                continue
            try:
                auto_pair_user_ids.add(int(raw_user_id))
            except ValueError:
                continue
        self._auto_pair_user_ids = auto_pair_user_ids
        self._guild_id = int(getattr(local_config, "guild_id", 0) or 0)
        self._control_channel_name = f"🤖-{_hostname_slug()}"
        self._reaction_new_session_enabled = bool(
            getattr(local_config, "reaction_new_session_enabled", True)
        )
        self._reaction_new_session_emoji = (
            getattr(local_config, "reaction_new_session_emoji", "✅") or "✅"
        )
        self._reaction_new_session_allow_plain_messages = bool(
            getattr(local_config, "reaction_new_session_allow_plain_messages", False)
        )
        self._reaction_shortcuts_completed: set[int] = set()
        self._reaction_shortcuts_in_progress: set[int] = set()
        self._pending_error_attachment_tasks: dict[str, asyncio.Task] = {}
        self._apply_auto_pair_users()

    @staticmethod
    def _parse_thread_id(raw_thread_id: object) -> int | None:
        try:
            thread_id = int(raw_thread_id or 0)
        except (TypeError, ValueError):
            return None
        return thread_id or None

    def _restore_thread_mappings_from_store(self) -> None:
        try:
            from tether.store import store
        except Exception:
            logger.exception("Failed to import store for Discord thread recovery")
            return

        for session in store.list_sessions():
            if getattr(session, "platform", None) != "discord":
                continue
            thread_id = self._parse_thread_id(
                getattr(session, "platform_thread_id", None)
            )
            if thread_id is None:
                continue
            self._thread_ids.setdefault(session.id, thread_id)

    def _hydrate_thread_binding(self, session_id: str) -> int | None:
        thread_id = self._thread_ids.get(session_id)
        if thread_id:
            return thread_id

        if self._get_session_info is not None:
            try:
                session_info = self._get_session_info(session_id)
            except Exception:
                logger.exception(
                    "Failed to fetch session info for Discord thread recovery",
                    session_id=session_id,
                )
            else:
                if isinstance(session_info, dict):
                    thread_id = self._parse_thread_id(
                        session_info.get("platform_thread_id")
                    )
                    if thread_id is not None:
                        self._thread_ids[session_id] = thread_id
                        return thread_id

        self._restore_thread_mappings_from_store()
        return self._thread_ids.get(session_id)

    async def on_output(
        self, session_id: str, text: str, metadata: dict | None = None
    ) -> None:
        self._hydrate_thread_binding(session_id)
        await super().on_output(session_id, text, metadata=metadata)
        await self._send_requested_output_attachments(session_id, metadata=metadata)

    async def on_status_change(
        self, session_id: str, status: str, metadata: dict | None = None
    ) -> None:
        self._hydrate_thread_binding(session_id)
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

    async def on_approval_request(self, session_id: str, request) -> None:
        self._hydrate_thread_binding(session_id)
        await super().on_approval_request(session_id, request)

    async def on_typing(self, session_id: str) -> None:
        self._hydrate_thread_binding(session_id)
        await super().on_typing(session_id)

    async def send_auto_approve_batch(
        self, session_id: str, items: list[tuple[str, str]]
    ) -> None:
        self._hydrate_thread_binding(session_id)
        await super().send_auto_approve_batch(session_id, items)

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

        thread_id = self._hydrate_thread_binding(session_id)
        if not thread_id:
            return

        thread = self._client.get_channel(thread_id)
        if thread is None:
            try:
                thread = await self._client.fetch_channel(thread_id)
            except Exception:
                logger.warning(
                    "Failed to fetch Discord thread for output attachments",
                    session_id=session_id,
                    thread_id=thread_id,
                )
                return

        failures: list[str] = []
        try:
            import discord
        except ImportError:
            logger.error("discord.py not installed for output attachment upload")
            return

        files = []
        for attachment in attachments:
            try:
                files.append(
                    discord.File(
                        attachment.path,
                        filename=attachment.filename,
                        description=attachment.title or attachment.filename,
                    )
                )
            except Exception:
                logger.exception(
                    "Failed to prepare Discord output attachment",
                    session_id=session_id,
                    attachment_path=attachment.path,
                )
                failures.append(attachment.filename)

        if files:
            try:
                await thread.send(files=files)
            except Exception:
                logger.exception(
                    "Failed to upload Discord output attachments",
                    session_id=session_id,
                )
                failures.extend(
                    attachment.filename
                    for attachment in attachments
                    if attachment.filename not in failures
                )

        if failures:
            try:
                await thread.send(
                    "Attachment upload failed: " + ", ".join(sorted(set(failures)))
                )
            except Exception:
                logger.exception(
                    "Failed to send Discord attachment failure notice",
                    session_id=session_id,
                )

    async def _send_error_attachment_bundle(
        self,
        session_id: str,
        metadata: dict | None = None,
    ) -> bool:
        if not self._client:
            return False

        thread_id = self._hydrate_thread_binding(session_id)
        if not thread_id:
            return False

        if not self._should_send_error_status(session_id):
            return True

        thread = self._client.get_channel(thread_id)
        if thread is None:
            try:
                thread = await self._client.fetch_channel(thread_id)
            except Exception:
                logger.warning(
                    "Failed to fetch Discord thread for error bundle",
                    session_id=session_id,
                    thread_id=thread_id,
                )
                return False

        bundle = build_error_debug_bundle(session_id, metadata=metadata)
        try:
            import discord

            files = [
                discord.File(
                    io.BytesIO(attachment.content.encode("utf-8")),
                    filename=attachment.filename,
                    description=attachment.title or attachment.filename,
                )
                for attachment in bundle.attachments
            ]
            await thread.send(bundle.message, files=files)
        except Exception:
            logger.exception(
                "Failed to send Discord error attachment bundle",
                session_id=session_id,
            )
            try:
                await thread.send("❌ Status: error")
            except Exception:
                logger.exception(
                    "Failed to send Discord fallback error status",
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
                    await super(DiscordBridge, self).on_status_change(
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

    async def start(self) -> None:
        """Initialize and start Discord client."""
        try:
            import discord
        except ImportError:
            logger.error(
                "discord.py not installed. Install with: pip install discord.py"
            )
            return

        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        if hasattr(intents, "guild_reactions"):
            intents.guild_reactions = True
        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_ready() -> None:
            logger.info(
                "Discord client ready",
                user=self._client.user,
                reaction_shortcuts=self._reaction_new_session_enabled,
            )

        @self._client.event
        async def on_message(message: Any) -> None:
            await self._handle_message(message)

        @self._client.event
        async def on_raw_reaction_add(payload: Any) -> None:
            await self._handle_raw_reaction_add(payload)

        asyncio.create_task(self._client.start(self._bot_token))

        logger.info(
            "Discord bridge initialized and starting", channel_id=self._channel_id
        )
        if not self._channel_id and self._pairing_code:
            logger.warning(
                "Discord bridge not configured with a control channel. Run !setup <code> in the desired channel.",
                code=self._pairing_code,
            )
        elif self._pairing_required and self._pairing_code:
            logger.warning(
                "Discord pairing enabled. DM the bot: !pair <code>",
                code=self._pairing_code,
            )

    async def on_session_removed(self, session_id: str) -> None:
        await self._cancel_pending_error_attachment_task(session_id)
        await super().on_session_removed(session_id)

    def _should_defer_new_message_to_reaction(self, text: str) -> bool:
        if not self._reaction_new_session_enabled:
            return False
        if "\n" not in text:
            return False
        try:
            return parse_reaction_shortcut_message(text) is not None
        except ReactionShortcutError:
            return True

    async def _handle_message(self, message: Any) -> None:
        """Route incoming Discord messages to commands or session input."""
        try:
            import discord
        except ImportError:
            return

        if message.author.bot:
            return

        text = message.content.strip()
        if not text:
            return

        if text.lower().startswith(("!pair", "!setup")):
            await self._dispatch_command(message, text)
            return

        if isinstance(message.channel, discord.Thread):
            if text.startswith("!"):
                await self._dispatch_command(message, text)
                return
            session_id = self._session_for_thread(message.channel.id)
            if not session_id:
                return
            if not self._is_authorized_user_id(getattr(message.author, "id", None)):
                await self._send_not_paired(message)
                return
            await self._forward_input(message, session_id, text)
            return

        if (
            self._channel_id
            and message.channel.id == self._channel_id
            and text.startswith("!")
        ):
            if text.lower().startswith(
                "!new"
            ) and self._should_defer_new_message_to_reaction(text):
                return
            await self._dispatch_command(message, text)
            return

        if not self._channel_id and text.lower().startswith("!setup"):
            await self._dispatch_command(message, text)

    def _session_for_thread(self, thread_id: int) -> str | None:
        session_id = super()._session_for_thread(thread_id)
        if session_id:
            return session_id
        self._restore_thread_mappings_from_store()
        return super()._session_for_thread(thread_id)

    async def create_thread(self, session_id: str, session_name: str) -> dict:
        if not self._client:
            raise RuntimeError("Discord client not initialized")

        channel = await self._ensure_control_channel()
        if not channel:
            raise RuntimeError(f"Discord channel {self._channel_id} not found")

        if hasattr(channel, "send"):
            return await self._create_public_thread_from_message(
                session_id=session_id,
                session_name=session_name,
                channel=channel,
            )

        logger.info(
            "Falling back to upstream Discord thread creation",
            session_id=session_id,
            channel_id=self._channel_id,
        )
        return await super().create_thread(session_id, session_name)

    def _prepare_thread_name_update(
        self, session_id: str, session_name: str
    ) -> tuple[str, str]:
        current_name = self._thread_names.get(session_id, "")
        desired_name = " ".join((session_name or "").split()) or "Session"
        desired_name = desired_name[:_DISCORD_THREAD_NAME_LIMIT]
        if current_name == desired_name:
            return current_name, desired_name

        if current_name:
            self._release_thread_name(session_id)
        resolved_name = self._pick_unique_thread_name(desired_name)
        self._reserve_thread_name(session_id, resolved_name)
        return current_name, resolved_name

    async def rename_thread(self, session_id: str, session_name: str) -> str:
        """Rename a Discord thread in place."""
        if not self._client:
            raise RuntimeError("Discord client not initialized")

        thread_id = self._hydrate_thread_binding(session_id)
        if not thread_id:
            raise RuntimeError(f"No Discord thread mapping for session {session_id}")

        thread = self._client.get_channel(thread_id)
        if thread is None:
            fetch_channel = getattr(self._client, "fetch_channel", None)
            if fetch_channel is not None:
                thread = await fetch_channel(thread_id)
        if thread is None or not hasattr(thread, "edit"):
            raise RuntimeError(f"Discord thread {thread_id} is not accessible")

        previous_name, resolved_name = self._prepare_thread_name_update(
            session_id, session_name
        )
        try:
            await thread.edit(name=resolved_name)
        except Exception as exc:
            if self._thread_names.get(session_id) == resolved_name:
                self._release_thread_name(session_id)
            if previous_name:
                self._reserve_thread_name(session_id, previous_name)
            raise RuntimeError(f"Failed to rename Discord thread: {exc}") from exc

        logger.info(
            "Renamed Discord thread",
            session_id=session_id,
            thread_id=thread_id,
            name=resolved_name,
        )
        return resolved_name

    def _persist_control_channel(self) -> None:
        self._ensure_pairing_state_loaded()
        if self._pairing_state is None:
            return
        self._pairing_state.control_channel_id = int(self._channel_id)
        self._pairing_state.paired_user_ids = set(self._paired_user_ids)
        save_pairing_state(path=self._pairing_state_path, state=self._pairing_state)

    def _apply_auto_pair_users(self) -> None:
        if not self._auto_pair_user_ids:
            return
        self._ensure_pairing_state_loaded()
        if self._pairing_state is None:
            return
        before = set(self._paired_user_ids)
        self._paired_user_ids.update(self._auto_pair_user_ids)
        if self._paired_user_ids == before:
            return
        self._pairing_state.paired_user_ids = set(self._paired_user_ids)
        save_pairing_state(path=self._pairing_state_path, state=self._pairing_state)
        logger.info(
            "Auto-paired Discord users from configuration",
            auto_pair_count=len(self._auto_pair_user_ids),
        )

    def _begin_reaction_shortcut(self, source_message_id: int) -> bool:
        if source_message_id in self._reaction_shortcuts_completed:
            return False
        if source_message_id in self._reaction_shortcuts_in_progress:
            return False
        self._reaction_shortcuts_in_progress.add(source_message_id)
        return True

    def _finish_reaction_shortcut(
        self, source_message_id: int, *, persist: bool
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

    async def _resolve_bootstrap_guild(self) -> Any | None:
        if not self._client:
            return None

        guilds = list(getattr(self._client, "guilds", []) or [])
        if self._guild_id:
            guild = self._client.get_guild(self._guild_id)
            if guild is None:
                logger.warning(
                    "Configured Discord guild not found for control channel bootstrap",
                    guild_id=self._guild_id,
                    guild_count=len(guilds),
                )
            return guild

        if len(guilds) == 1:
            return guilds[0]

        if len(guilds) > 1:
            logger.warning(
                "Discord control channel bootstrap needs DISCORD_GUILD_ID when the bot is in multiple guilds",
                guild_count=len(guilds),
            )
            return None

        logger.warning("Discord control channel bootstrap found no accessible guilds")
        return None

    async def _ensure_control_channel(self) -> Any | None:
        if not self._client:
            return None

        if self._channel_id:
            channel = self._client.get_channel(self._channel_id)
            if channel is not None:
                return channel
            fetch_channel = getattr(self._client, "fetch_channel", None)
            if fetch_channel is not None:
                try:
                    channel = await fetch_channel(self._channel_id)
                except Exception:
                    logger.warning(
                        "Configured Discord control channel is not accessible; retrying bootstrap",
                        channel_id=self._channel_id,
                    )
                else:
                    return channel

        guild = await self._resolve_bootstrap_guild()
        if guild is None:
            return None

        for channel in getattr(guild, "text_channels", []) or []:
            if getattr(channel, "name", None) != self._control_channel_name:
                continue
            self._channel_id = int(channel.id)
            self._persist_control_channel()
            logger.info(
                "Using existing Discord control channel",
                guild_id=getattr(guild, "id", 0),
                channel_id=self._channel_id,
                channel_name=self._control_channel_name,
            )
            return channel

        topic = (
            f"Tether control channel for {socket.gethostname().split('.', 1)[0]}. "
            "Session threads are created from here automatically."
        )
        channel = await guild.create_text_channel(
            name=self._control_channel_name,
            topic=topic[:1024],
        )
        self._channel_id = int(channel.id)
        self._persist_control_channel()
        logger.info(
            "Created Discord control channel",
            guild_id=getattr(guild, "id", 0),
            channel_id=self._channel_id,
            channel_name=self._control_channel_name,
        )
        return channel

    async def _handle_raw_reaction_add(self, payload: Any) -> None:
        """Create and start a new session from a reacted control-channel message."""
        if not self._reaction_new_session_enabled or not self._client:
            return

        channel_id = int(getattr(payload, "channel_id", 0) or 0)
        if not self._channel_id or channel_id != int(self._channel_id):
            return

        source_message_id = int(getattr(payload, "message_id", 0) or 0)
        if not source_message_id:
            return

        user_id = int(getattr(payload, "user_id", 0) or 0)
        client_user_id = int(getattr(getattr(self._client, "user", None), "id", 0) or 0)
        if client_user_id and user_id == client_user_id:
            return
        if not self._is_authorized_user_id(user_id):
            return

        emoji_name = getattr(getattr(payload, "emoji", None), "name", None) or str(
            getattr(payload, "emoji", "") or ""
        )
        if not reaction_matches(self._reaction_new_session_emoji, emoji_name):
            return
        if not self._begin_reaction_shortcut(source_message_id):
            return

        persist = False
        channel: Any | None = None
        try:
            channel = self._client.get_channel(channel_id)
            if channel is None:
                fetch_channel = getattr(self._client, "fetch_channel", None)
                if fetch_channel is not None:
                    channel = await fetch_channel(channel_id)
            if channel is None or not hasattr(channel, "fetch_message"):
                return

            message = await channel.fetch_message(source_message_id)
            if getattr(getattr(message, "author", None), "bot", False):
                return

            shortcut = parse_reaction_shortcut_message(
                getattr(message, "content", ""),
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
                platform="discord",
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
            reply = f"✅ New {agent_label} session created in {dir_short} from a checkmark reaction."
            try:
                thread_id = int(session.get("platform_thread_id") or 0)
            except Exception:
                thread_id = 0
            if thread_id:
                reply += f"\n🧵 Open thread: <#{thread_id}>"
            await channel.send(reply[:_DISCORD_STARTER_TEXT_LIMIT])
        except (ReactionShortcutError, ValueError) as exc:
            if channel is not None:
                await channel.send(str(exc))
        except Exception as exc:
            logger.exception(
                "Failed to create Discord session from reaction",
                source_message_id=source_message_id,
            )
            if channel is not None:
                await channel.send(f"Failed to create session from reaction: {exc}")
        finally:
            self._finish_reaction_shortcut(source_message_id, persist=persist)

    async def _create_public_thread_from_message(
        self,
        *,
        session_id: str,
        session_name: str,
        channel: Any,
    ) -> dict:
        try:
            self._reserve_thread_name(session_id, session_name)

            starter_text = (
                f"🧵 Tether session: **{session_name[:80]}**\n"
                "This starter message keeps the thread visible in this machine channel."
            )
            starter_message = await channel.send(
                starter_text[:_DISCORD_STARTER_TEXT_LIMIT]
            )
            thread = await starter_message.create_thread(
                name=session_name[:_DISCORD_THREAD_NAME_LIMIT],
                auto_archive_duration=_DISCORD_AUTO_ARCHIVE_MINUTES,
            )

            thread_id = thread.id
            self._thread_ids[session_id] = thread_id
            try:
                await thread.send(
                    "Tether session thread.\n"
                    "Send a message here to provide input. Use `!stop` to interrupt, `!usage` for token usage."
                )
            except Exception:
                pass

            logger.info(
                "Created visible Discord thread",
                session_id=session_id,
                thread_id=thread_id,
                name=session_name,
                channel_id=self._channel_id,
            )
            return {
                "thread_id": str(thread_id),
                "platform": "discord",
            }
        except Exception as exc:
            logger.exception(
                "Failed to create visible Discord thread", session_id=session_id
            )
            if self._thread_names.get(session_id) == session_name:
                self._release_thread_name(session_id)
            raise RuntimeError(f"Failed to create Discord thread: {exc}") from exc
