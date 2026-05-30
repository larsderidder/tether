"""Tether-local Telegram bridge wrapper with richer output formatting."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

import structlog
from agent_tether.telegram.bot import TelegramBridge as UpstreamTelegramBridge

from tether.bridges.attachments import attachments_from_metadata
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
            from telegram.ext import MessageHandler, filters
        except ImportError:
            return

        self._app.add_handler(
            MessageHandler(
                (filters.PHOTO | filters.ATTACHMENT) & filters.ChatType.SUPERGROUP,
                self._handle_media_message,
            )
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
            declared_mime_type = getattr(document, "mime_type", None) if document else "image/jpeg"
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

        images, _ = await self._collect_message_media(update, "unknown", collect_files=False)
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
                "💡 Send images in a session topic to interact with that agent."
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
