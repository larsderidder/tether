"""Tether-local Telegram bridge wrapper with richer output formatting."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import html
from typing import Any

import structlog
from agent_tether.telegram.bot import TelegramBridge as UpstreamTelegramBridge

from tether.bridges.attachments import attachments_from_metadata
from tether.bridges.base import _EXTERNAL_MAX_FETCH, _EXTERNAL_PAGE_SIZE, _relative_time
from tether.bridges.compact_api import compact_session
from tether.bridges.dedupe import (
    ShortLivedMessageDedupe,
    is_obvious_telegram_bot_loop,
    telegram_update_key,
)
from tether.bridges.image_io import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_MESSAGE,
    make_bridge_image,
)
from tether.bridges.media_io import (
    MAX_MEDIA_BYTES,
    BridgeMediaFile,
    append_media_file_references,
    download_with_media_policy,
    store_bridge_media_file,
    supported_media_type,
)
from tether.bridges.rich_output import render_telegram_messages
from tether.bridges.retry import with_bridge_send_retry

logger = structlog.get_logger(__name__)
_TELEGRAM_MEDIA_GROUP_DEBOUNCE_S = 0.7


@dataclass
class _TelegramMediaGroupBuffer:
    """Pending Telegram album media before dispatching as one turn."""

    session_id: str
    topic_id: int
    message: Any
    texts: list[str] = field(default_factory=list)
    images: list[dict[str, str]] = field(default_factory=list)
    skipped_count: int = 0
    total_count: int = 0


class TelegramBridge(UpstreamTelegramBridge):
    """Render tool calls and pass Telegram images through to sessions."""

    @staticmethod
    def _agent_to_adapter(raw: str) -> str | None:
        """Map user-friendly agent names to local adapter names."""

        if (raw or "").strip().lower() == "runbook":
            return "runbook"
        return UpstreamTelegramBridge._agent_to_adapter(raw)

    @staticmethod
    def _adapter_label(adapter: str | None) -> str | None:
        """Map local adapter names to user-friendly labels."""

        if adapter == "runbook":
            return "Runbook"
        return UpstreamTelegramBridge._adapter_label(adapter)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._media_group_buffers: dict[str, _TelegramMediaGroupBuffer] = {}
        self._media_group_tasks: dict[str, asyncio.Task] = {}
        self._message_dedupe = ShortLivedMessageDedupe()

    async def start(self) -> None:
        """Start Telegram and register a media handler for session topics."""

        await super().start()
        if not self._app:
            return

        try:
            from telegram.ext import CommandHandler, MessageHandler, filters
        except ImportError:
            return

        self._app.add_handler(CommandHandler("compact", self._cmd_compact))
        self._app.add_handler(
            MessageHandler(
                (filters.PHOTO | filters.ATTACHMENT) & filters.ChatType.SUPERGROUP,
                self._handle_media_message,
            )
        )

    @staticmethod
    def _clip_mono(text: str, width: int) -> str:
        """Clip text for a fixed-width Telegram listing."""
        text = " ".join(str(text or "").split())
        if len(text) <= width:
            return text
        return text[: max(1, width - 1)].rstrip() + "…"

    def _format_external_page_html(self, page: int) -> tuple[str, int, int]:
        """Format external sessions for Telegram with aligned monospace rows."""
        sessions = self._external_view or []
        if not sessions:
            if self._external_query:
                query = html.escape(self._external_query)
                return (
                    f"No external sessions match directory search: <code>{query}</code>\n\n"
                    "Try a different query, or run /list to clear the search.",
                    1,
                    1,
                )
            return (
                "No external sessions found.\n\n"
                "Start a Claude Code or Codex session first, then use /list to see it.",
                1,
                1,
            )

        total = len(sessions)
        total_pages = max(1, (total + _EXTERNAL_PAGE_SIZE - 1) // _EXTERNAL_PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * _EXTERNAL_PAGE_SIZE
        end = min(start + _EXTERNAL_PAGE_SIZE, total)

        title = f"External Sessions (page {page}/{total_pages})"
        if self._external_query:
            title += f" [search: {self._external_query}]"

        rows: list[str] = []
        for idx in range(start, end):
            session = sessions[idx]
            number = idx + 1
            directory = str(session.get("directory") or "")
            dir_short = directory.rsplit("/", 1)[-1] if directory else "?"
            runner = str(session.get("runner_type") or "?")
            age = _relative_time(str(session.get("last_activity") or ""))
            prompt = str(
                session.get("last_prompt") or session.get("first_prompt") or ""
            )
            header = f"{number:>2}. {self._clip_mono(dir_short, 28):<28} ({runner})"
            if age:
                header += f" • {age}"
            rows.append(header.rstrip())
            if prompt:
                rows.append(f"    ↳ {self._clip_mono(prompt, 58)}")

        lines = [
            html.escape(title) + ":",
            "<pre>" + html.escape("\n".join(rows)) + "</pre>",
        ]
        if (
            not self._external_query
            and len(self._cached_external) == _EXTERNAL_MAX_FETCH
        ):
            lines.append(f"Showing up to {_EXTERNAL_MAX_FETCH} sessions (API limit).")
        lines.append("/attach &lt;number&gt; to attach.")
        return "\n\n".join(lines), page, total_pages

    async def _cmd_list(self, update: Any, context: Any) -> None:
        """Handle /list with Telegram-friendly aligned formatting."""
        page = 1
        query: str | None = None
        args = getattr(context, "args", None) or []
        user_id = getattr(getattr(update, "effective_user", None), "id", None)
        if args:
            first = args[0]
            try:
                page = int(first)
                query = self._external_query
            except Exception:
                query = " ".join(args).strip()
                page = 1

        try:
            await self._refresh_external_cache()
            if not args:
                self._set_external_view(None)
            else:
                self._set_external_view(query)
        except Exception:
            logger.exception("Failed to fetch external sessions")
            await update.message.reply_text("Failed to list external sessions.")
            return

        if user_id is not None:
            self._external_view_by_user[int(user_id)] = list(self._external_view)

        text, page, total_pages = self._format_external_page_html(page)
        reply_markup = self._external_pagination_markup(page, total_pages)
        await update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )

    async def _handle_list_callback_query(self, update: Any, context: Any) -> None:
        """Handle Telegram pagination with aligned HTML formatting."""
        query = update.callback_query
        if not query or not getattr(query, "data", None):
            return

        data = query.data
        await query.answer()

        if data == "list:refresh":
            try:
                await self._refresh_external_cache()
            except Exception:
                logger.exception("Failed to refresh external sessions")
                with contextlib.suppress(Exception):
                    await query.edit_message_text(
                        "Failed to refresh external sessions."
                    )
                return
            self._set_external_view(self._external_query)
            page = 1
        else:
            try:
                _, kind, value = data.split(":", 2)
                if kind != "page":
                    return
                page = int(value)
            except Exception:
                return

        if not self._cached_external:
            try:
                await self._refresh_external_cache()
            except Exception:
                logger.exception("Failed to fetch external sessions for pagination")
                with contextlib.suppress(Exception):
                    await query.edit_message_text(
                        "Failed to list external sessions. Run /list again."
                    )
                return
            self._set_external_view(self._external_query)

        text, page, total_pages = self._format_external_page_html(page)
        reply_markup = self._external_pagination_markup(page, total_pages)
        try:
            await query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        except Exception:
            try:
                await query.message.reply_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("Failed to send external pagination message")

    async def _cmd_compact(self, update: Any, context: Any) -> None:
        """Handle /compact in a session topic."""
        message = getattr(update, "message", None)
        if message is None:
            return
        topic_id = getattr(message, "message_thread_id", None)
        if not topic_id:
            await message.reply_text("Use this command inside a session topic.")
            return
        session_id = self._state.get_session_for_topic(topic_id)
        if not session_id:
            await message.reply_text("No session linked to this topic.")
            return
        custom_instructions = " ".join(getattr(context, "args", []) or []).strip()
        try:
            await compact_session(session_id, custom_instructions or None)
            await message.reply_text("🧹 Compaction requested.")
        except Exception as exc:
            logger.exception(
                "Failed to compact Telegram session", session_id=session_id
            )
            await message.reply_text(f"Failed to compact session: {exc}")

    async def _cmd_help(self, update: Any, context: Any) -> None:
        """Handle /help with local commands included."""
        await super()._cmd_help(update, context)
        await update.message.reply_text(
            "Extra command: /compact [instructions] — Compact pi context for this session"
        )

    async def _collect_message_media(
        self,
        update: object,
        session_id: str,
        *,
        collect_files: bool = True,
    ) -> tuple[list[dict[str, str]], list[BridgeMediaFile]]:
        """Download and validate supported Telegram attachments."""

        message = getattr(update, "message", None)
        if message is None:
            return [], []

        photos = list(getattr(message, "photo", []) or [])
        document = getattr(message, "document", None)
        audio = getattr(message, "audio", None)
        video = getattr(message, "video", None)
        image_ref = photos[-1] if photos else document
        if image_ref is not None:
            declared_mime_type = (
                getattr(document, "mime_type", None) if document else "image/jpeg"
            )
            if photos or str(declared_mime_type or "").lower().startswith("image/"):
                filename = getattr(document, "file_name", None) if document else None
                size = int(getattr(image_ref, "file_size", 0) or 0)
                if size > MAX_IMAGE_BYTES:
                    await message.reply_text(
                        f"⚠️ Skipped image: image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
                    )
                    return [], []
                try:
                    telegram_file = await image_ref.get_file()
                    data = await download_with_media_policy(
                        telegram_file.download_as_bytearray,
                        platform="telegram",
                        url=getattr(telegram_file, "file_path", None),
                    )
                    image = make_bridge_image(
                        data,
                        declared_mime_type=declared_mime_type,
                        filename=filename,
                    )
                except ValueError as exc:
                    await message.reply_text(f"⚠️ Skipped image: {exc}")
                    return [], []
                except Exception:
                    logger.exception("Failed to read Telegram image attachment")
                    await message.reply_text("⚠️ Failed to read an image attachment.")
                    return [], []
                return [image.as_api_payload()], []

        if not collect_files:
            return [], []

        media_ref = document or audio or video
        if media_ref is None:
            return [], []
        mime_type = getattr(media_ref, "mime_type", None)
        if not supported_media_type(mime_type):
            return [], []
        size = int(getattr(media_ref, "file_size", 0) or 0)
        if size > MAX_MEDIA_BYTES:
            await message.reply_text(
                f"⚠️ Skipped attachment: file is larger than {MAX_MEDIA_BYTES // (1024 * 1024)} MB"
            )
            return [], []
        try:
            telegram_file = await media_ref.get_file()
            data = await download_with_media_policy(
                telegram_file.download_as_bytearray,
                platform="telegram",
                url=getattr(telegram_file, "file_path", None),
            )
            media_file = store_bridge_media_file(
                session_id=session_id,
                data=data,
                filename=getattr(media_ref, "file_name", None),
                mime_type=mime_type,
            )
        except ValueError as exc:
            await message.reply_text(f"⚠️ Skipped attachment: {exc}")
            return [], []
        except Exception:
            logger.exception("Failed to read Telegram media attachment")
            await message.reply_text("⚠️ Failed to read an attachment.")
            return [], []
        return [], [media_file]

    async def _collect_message_images(self, update: object) -> list[dict[str, str]]:
        """Download and validate supported Telegram image attachments."""

        images, _ = await self._collect_message_media(
            update, "unknown", collect_files=False
        )
        return images

    async def _handle_media_message(self, update: object, context: object) -> None:
        """Handle Telegram photos and forward them as native image input."""

        message = getattr(update, "message", None)
        if message is None:
            return
        if self._should_ignore_inbound_media(update):
            return

        topic_id = getattr(message, "message_thread_id", None)
        if not topic_id:
            await message.reply_text(
                "💡 Send attachments in a session topic to interact with that agent."
            )
            return

        session_id = self._state.get_session_for_topic(topic_id)
        if not session_id:
            await message.reply_text("⚠️ No active session is linked to this topic.")
            return

        group_id = str(getattr(message, "media_group_id", "") or "").strip()
        if group_id:
            await self._buffer_media_group(update, session_id, topic_id, group_id)
            return

        text = (getattr(message, "caption", None) or "").strip()
        images, files = await self._collect_message_media(update, session_id)
        text = append_media_file_references(text, files)
        await self._send_media_input(
            session_id=session_id,
            topic_id=topic_id,
            text=text,
            images=images,
            files=files,
            message=message,
        )

    def _should_ignore_inbound_media(self, update: object) -> bool:
        """Suppress duplicate Telegram media deliveries and obvious bot loops."""

        if is_obvious_telegram_bot_loop(update):
            return True
        if self._message_dedupe.seen_recently(telegram_update_key(update)):
            message = getattr(update, "message", None)
            logger.debug(
                "Dropped duplicate Telegram inbound media",
                message_id=getattr(message, "message_id", None),
                chat_id=getattr(getattr(message, "chat", None), "id", None),
            )
            return True
        return False

    async def _send_media_input(
        self,
        *,
        session_id: str,
        topic_id: int,
        text: str,
        images: list[dict[str, str]],
        files: list[BridgeMediaFile],
        message: Any,
    ) -> None:
        """Forward collected media input to a session."""

        if not images and not files:
            return
        if not text:
            text = "Please look at this attachment."

        try:
            if images:
                await self._callbacks.send_input(session_id, text, images=images)
            else:
                await self._callbacks.send_input(session_id, text)
            logger.info(
                "Forwarded media input from Telegram",
                session_id=session_id,
                topic_id=topic_id,
                image_count=len(images),
                file_count=len(files),
            )
        except Exception as exc:
            logger.exception(
                "Failed to forward Telegram media input",
                session_id=session_id,
                topic_id=topic_id,
            )
            await message.reply_text(f"❌ Failed to send input: {exc}")

    async def _send_image_input(
        self,
        *,
        session_id: str,
        topic_id: int,
        text: str,
        images: list[dict[str, str]],
        message: Any,
    ) -> None:
        """Forward collected image input to a session."""

        await self._send_media_input(
            session_id=session_id,
            topic_id=topic_id,
            text=text,
            images=images,
            files=[],
            message=message,
        )

    async def _buffer_media_group(
        self,
        update: object,
        session_id: str,
        topic_id: int,
        group_id: str,
    ) -> None:
        """Buffer Telegram album entries and dispatch them as one turn."""

        message = getattr(update, "message", None)
        if message is None:
            return

        chat_id = getattr(getattr(message, "chat", None), "id", "")
        key = f"{chat_id}:{topic_id}:{group_id}"
        buffer = self._media_group_buffers.setdefault(
            key,
            _TelegramMediaGroupBuffer(
                session_id=session_id,
                topic_id=topic_id,
                message=message,
            ),
        )
        text = (getattr(message, "caption", None) or "").strip()
        if text:
            buffer.texts.append(text)

        buffer.total_count += 1
        if len(buffer.images) >= MAX_IMAGES_PER_MESSAGE:
            buffer.skipped_count += 1
        else:
            images = await self._collect_message_images(update)
            remaining = MAX_IMAGES_PER_MESSAGE - len(buffer.images)
            accepted = images[:remaining]
            buffer.images.extend(accepted)
            if len(accepted) < len(images) or not images:
                buffer.skipped_count += 1

        existing = self._media_group_tasks.pop(key, None)
        if existing and not existing.done():
            existing.cancel()

        async def _delayed_flush() -> None:
            try:
                await asyncio.sleep(_TELEGRAM_MEDIA_GROUP_DEBOUNCE_S)
            except asyncio.CancelledError:
                return
            self._media_group_tasks.pop(key, None)
            await self._flush_media_group(key)

        self._media_group_tasks[key] = asyncio.create_task(_delayed_flush())

    async def _flush_media_group(self, key: str) -> None:
        """Send a buffered Telegram album to the session."""

        pending_task = self._media_group_tasks.pop(key, None)
        if pending_task and pending_task is not asyncio.current_task():
            pending_task.cancel()

        buffer = self._media_group_buffers.pop(key, None)
        if buffer is None:
            return
        if buffer.skipped_count > 0:
            await self._send_media_group_skip_warning(buffer)

        text = "\n".join(buffer.texts).strip()
        await self._send_image_input(
            session_id=buffer.session_id,
            topic_id=buffer.topic_id,
            text=text,
            images=buffer.images,
            message=buffer.message,
        )

    async def _send_media_group_skip_warning(
        self,
        buffer: _TelegramMediaGroupBuffer,
    ) -> None:
        """Notify Telegram users when an album was only partially accepted."""

        total = max(buffer.total_count, len(buffer.images) + buffer.skipped_count)
        skipped = buffer.skipped_count
        was_or_were = "was" if skipped == 1 else "were"
        try:
            await buffer.message.reply_text(
                f"⚠️ Received {len(buffer.images)} of {total} images; {skipped} {was_or_were} skipped."
            )
        except Exception:
            logger.exception(
                "Failed to send Telegram media group warning",
                session_id=buffer.session_id,
                topic_id=buffer.topic_id,
            )

    async def on_output(
        self, session_id: str, text: str, metadata: dict | None = None
    ) -> None:
        is_final = bool(metadata and metadata.get("final"))
        if is_final:
            self._stop_typing(session_id)
        if not self._app:
            logger.warning("Telegram app not initialized")
            return

        topic_id = self._state.get_topic_for_session(session_id)
        if not topic_id:
            logger.warning("No Telegram topic for session", session_id=session_id)
            return

        messages = render_telegram_messages(text, metadata=metadata) or [text]
        for message in messages:
            try:
                await with_bridge_send_retry(
                    "telegram.output",
                    lambda message=message: self._app.bot.send_message(
                        chat_id=self._forum_group_id,
                        message_thread_id=topic_id,
                        text=message,
                        parse_mode="HTML",
                    ),
                )
            except Exception:
                try:
                    await with_bridge_send_retry(
                        "telegram.output_fallback",
                        lambda message=message: self._app.bot.send_message(
                            chat_id=self._forum_group_id,
                            message_thread_id=topic_id,
                            text=message[:4096],
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Failed to send Telegram message",
                        session_id=session_id,
                        topic_id=topic_id,
                    )

        await self._send_output_attachments(session_id, topic_id, metadata=metadata)

    async def _send_output_attachments(
        self,
        session_id: str,
        topic_id: int,
        *,
        metadata: dict | None = None,
    ) -> None:
        """Upload runner-published attachments to Telegram."""

        attachments = attachments_from_metadata(
            metadata,
            max_count=MAX_IMAGES_PER_MESSAGE,
        )
        if not attachments or not self._app:
            return

        for attachment in attachments:
            try:
                with attachment.path.open("rb") as handle:
                    if attachment.send_as_image:
                        await with_bridge_send_retry(
                            "telegram.output_photo",
                            lambda handle=handle, attachment=attachment: self._app.bot.send_photo(
                                chat_id=self._forum_group_id,
                                message_thread_id=topic_id,
                                photo=handle,
                                caption=attachment.caption,
                            ),
                        )
                    else:
                        await with_bridge_send_retry(
                            "telegram.output_document",
                            lambda handle=handle, attachment=attachment: self._app.bot.send_document(
                                chat_id=self._forum_group_id,
                                message_thread_id=topic_id,
                                document=handle,
                                filename=attachment.filename,
                                caption=attachment.caption,
                            ),
                        )
            except Exception:
                logger.exception(
                    "Failed to send Telegram output attachment",
                    session_id=session_id,
                    attachment_path=str(attachment.path),
                )
                with contextlib.suppress(Exception):
                    await with_bridge_send_retry(
                        "telegram.attachment_failure_notice",
                        lambda attachment=attachment: self._app.bot.send_message(
                            chat_id=self._forum_group_id,
                            message_thread_id=topic_id,
                            text=f"Attachment upload failed: {attachment.filename}",
                        ),
                    )
