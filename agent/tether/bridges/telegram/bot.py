"""Tether-local Telegram bridge wrapper with richer output formatting."""

from __future__ import annotations

import contextlib
import mimetypes
from pathlib import Path

import structlog
from agent_tether.telegram.bot import TelegramBridge as UpstreamTelegramBridge

from tether.bridges.image_io import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_MESSAGE,
    make_bridge_image,
)
from tether.bridges.rich_output import render_telegram_messages
from tether.bridges.retry import with_bridge_send_retry
from tether.output_postprocess import PublishedAttachment

logger = structlog.get_logger(__name__)


class TelegramBridge(UpstreamTelegramBridge):
    """Render tool calls and pass Telegram images through to sessions."""

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
                filters.PHOTO & filters.ChatType.SUPERGROUP,
                self._handle_media_message,
            )
        )

    async def _collect_message_images(self, update: object) -> list[dict[str, str]]:
        """Download and validate supported Telegram image attachments."""

        message = getattr(update, "message", None)
        if message is None:
            return []

        photos = list(getattr(message, "photo", []) or [])
        if not photos:
            return []

        largest = photos[-1]
        size = int(getattr(largest, "file_size", 0) or 0)
        if size > MAX_IMAGE_BYTES:
            await message.reply_text(
                f"⚠️ Skipped image: image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
            )
            return []

        try:
            telegram_file = await largest.get_file()
            data = bytes(await telegram_file.download_as_bytearray())
            image = make_bridge_image(data, declared_mime_type="image/jpeg")
        except ValueError as exc:
            await message.reply_text(f"⚠️ Skipped image: {exc}")
            return []
        except Exception:
            logger.exception("Failed to read Telegram image attachment")
            await message.reply_text("⚠️ Failed to read an image attachment.")
            return []
        return [image.as_api_payload()]

    async def _handle_media_message(self, update: object, context: object) -> None:
        """Handle Telegram photos and forward them as native image input."""

        message = getattr(update, "message", None)
        if message is None:
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

        text = (getattr(message, "caption", None) or "").strip()
        images = await self._collect_message_images(update)
        if not images:
            return
        if not text:
            text = "Please look at this image."

        try:
            if images:
                await self._callbacks.send_input(session_id, text, images=images)
            else:
                await self._callbacks.send_input(session_id, text)
            logger.info(
                "Forwarded image input from Telegram",
                session_id=session_id,
                topic_id=topic_id,
                image_count=len(images),
            )
        except Exception as exc:
            logger.exception(
                "Failed to forward Telegram image input",
                session_id=session_id,
                topic_id=topic_id,
            )
            await message.reply_text(f"❌ Failed to send input: {exc}")

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

        attachments = [
            attachment
            for attachment in (
                PublishedAttachment.from_metadata(item)
                for item in (metadata or {}).get("attachments") or []
            )
            if attachment is not None
        ][:MAX_IMAGES_PER_MESSAGE]
        if not attachments or not self._app:
            return

        for attachment in attachments:
            attachment_path = Path(attachment.path)
            media_type = mimetypes.guess_type(attachment.filename)[0] or ""
            try:
                with attachment_path.open("rb") as handle:
                    if media_type.startswith("image/"):
                        await with_bridge_send_retry(
                            "telegram.output_photo",
                            lambda handle=handle, attachment=attachment: self._app.bot.send_photo(
                                chat_id=self._forum_group_id,
                                message_thread_id=topic_id,
                                photo=handle,
                                caption=attachment.title or attachment.filename,
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
                                caption=attachment.title or attachment.filename,
                            ),
                        )
            except Exception:
                logger.exception(
                    "Failed to send Telegram output attachment",
                    session_id=session_id,
                    attachment_path=str(attachment_path),
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
