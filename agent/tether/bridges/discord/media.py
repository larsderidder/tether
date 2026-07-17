"""Discord media and attachment helpers."""

from __future__ import annotations

import asyncio
import contextlib
import io
import mimetypes
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from tether.bridges.attachments import attachments_from_metadata
from tether.bridges.debug_attachments import build_error_debug_bundle
from tether.bridges.image_io import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_MESSAGE,
    make_bridge_image,
)
from tether.bridges.media_io import (
    MAX_MEDIA_BYTES,
    MAX_MEDIA_FILES_PER_MESSAGE,
    BridgeMediaFile,
    download_with_media_policy,
    store_bridge_media_file,
    supported_media_type,
)
from tether.bridges.retry import with_bridge_send_retry

logger = structlog.get_logger(__name__)


class DiscordMediaHandler:
    """Handle Discord bridge media collection and attachment delivery."""

    def __init__(
        self,
        *,
        get_client: Callable[[], Any | None],
        hydrate_thread_binding: Callable[[str], int | None],
        should_send_error_status: Callable[[str], bool],
        send_plain_error_status: Callable[[str, dict | None], Awaitable[None]],
        error_attachment_delay_s: float,
    ) -> None:
        """Create a media helper bound to a Discord bridge instance."""

        self._get_client = get_client
        self._hydrate_thread_binding = hydrate_thread_binding
        self._should_send_error_status = should_send_error_status
        self._send_plain_error_status = send_plain_error_status
        self._error_attachment_delay_s = error_attachment_delay_s
        self._pending_error_attachment_tasks: dict[str, asyncio.Task] = {}

    async def message_attachments(self, message: Any) -> list[Any]:
        """Return direct, replied-to, and forwarded Discord attachments."""

        attachments = list(getattr(message, "attachments", []) or [])
        attachments.extend(self.forwarded_message_attachments(message))
        reference = getattr(message, "reference", None)
        resolved = getattr(reference, "resolved", None)
        if resolved is not None:
            attachments.extend(list(getattr(resolved, "attachments", []) or []))
            return attachments

        message_id = getattr(reference, "message_id", None)
        fetch_message = getattr(
            getattr(message, "channel", None), "fetch_message", None
        )
        if message_id and fetch_message is not None:
            try:
                fetched = await fetch_message(message_id)
            except Exception:
                logger.debug(
                    "Failed to hydrate Discord referenced message for images",
                    message_id=message_id,
                    exc_info=True,
                )
            else:
                attachments.extend(list(getattr(fetched, "attachments", []) or []))
        return attachments

    @staticmethod
    def forwarded_message_attachments(message: Any) -> list[Any]:
        """Return attachments from Discord forwarded message snapshots."""

        raw_data = getattr(message, "rawData", None)
        candidates = [
            getattr(raw_data, "message_snapshots", None),
            getattr(message, "message_snapshots", None),
            getattr(message, "messageSnapshots", None),
        ]
        snapshots = next(
            (candidate for candidate in candidates if isinstance(candidate, list)),
            [],
        )

        attachments: list[Any] = []
        for snapshot in snapshots:
            snapshot_message = None
            if isinstance(snapshot, dict):
                snapshot_message = snapshot.get("message")
            else:
                snapshot_message = getattr(snapshot, "message", None)
            if isinstance(snapshot_message, dict):
                snapshot_attachments = snapshot_message.get("attachments") or []
            else:
                snapshot_attachments = (
                    getattr(snapshot_message, "attachments", []) or []
                )
            if isinstance(snapshot_attachments, list):
                attachments.extend(snapshot_attachments)
        return attachments

    def message_has_media(self, message: Any) -> bool:
        """Return true when a Discord message may carry bridgeable media."""

        if getattr(message, "attachments", None):
            return True
        if self.forwarded_message_attachments(message):
            return True
        reference = getattr(message, "reference", None)
        if reference is None:
            return False
        resolved = getattr(reference, "resolved", None)
        if resolved is not None and getattr(resolved, "attachments", None):
            return True
        return bool(getattr(reference, "message_id", None))

    async def collect_message_media(
        self,
        message: Any,
        session_id: str,
        *,
        collect_files: bool = True,
    ) -> tuple[list[dict[str, str]], list[BridgeMediaFile]]:
        """Download and validate supported Discord attachments."""

        images: list[dict[str, str]] = []
        files: list[BridgeMediaFile] = []
        for attachment in await self.message_attachments(message):
            filename = getattr(attachment, "filename", None)
            content_type = getattr(attachment, "content_type", None)
            guessed_type = mimetypes.guess_type(str(filename or ""))[0] or ""
            content_type_text = str(content_type or guessed_type or "").lower()
            generic_type = content_type_text in {
                "",
                "application/octet-stream",
                "binary/octet-stream",
            }
            effective_type = content_type_text
            if generic_type and guessed_type:
                effective_type = guessed_type.lower()
            size = int(getattr(attachment, "size", 0) or 0)
            if size <= 0:
                continue
            if effective_type.startswith("image/") or generic_type:
                image = await self._read_image_attachment(
                    message,
                    attachment,
                    effective_type=effective_type,
                    filename=filename,
                    images=images,
                    size=size,
                )
                if image is not None:
                    images.append(image)
                continue

            if not collect_files or not supported_media_type(effective_type):
                continue
            media_file = await self._read_media_attachment(
                message,
                attachment,
                session_id=session_id,
                effective_type=effective_type,
                filename=filename,
                files=files,
                size=size,
            )
            if media_file is not None:
                files.append(media_file)
        return images, files

    async def collect_message_images(self, message: Any) -> list[dict[str, str]]:
        """Download and validate supported Discord image attachments."""

        images, _ = await self.collect_message_media(
            message,
            "unknown",
            collect_files=False,
        )
        return images

    async def _read_image_attachment(
        self,
        message: Any,
        attachment: Any,
        *,
        effective_type: str,
        filename: Any,
        images: list[dict[str, str]],
        size: int,
    ) -> dict[str, str] | None:
        """Read one Discord image attachment into an API payload."""

        if len(images) >= MAX_IMAGES_PER_MESSAGE:
            await message.channel.send(
                f"⚠️ Skipped image: maximum {MAX_IMAGES_PER_MESSAGE} images per message."
            )
            return None
        if size > MAX_IMAGE_BYTES:
            await message.channel.send(
                f"⚠️ Skipped image: image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
            )
            return None
        try:
            data = await download_with_media_policy(
                attachment.read,
                platform="discord",
                url=getattr(attachment, "url", None)
                or getattr(attachment, "proxy_url", None),
            )
            image = make_bridge_image(
                data,
                declared_mime_type=effective_type,
                filename=filename,
            )
        except ValueError as exc:
            if effective_type.startswith("image/"):
                await message.channel.send(f"⚠️ Skipped image: {exc}")
            return None
        except Exception:
            logger.exception("Failed to read Discord image attachment")
            await message.channel.send("⚠️ Failed to read an image attachment.")
            return None
        return image.as_api_payload()

    async def _read_media_attachment(
        self,
        message: Any,
        attachment: Any,
        *,
        session_id: str,
        effective_type: str,
        filename: Any,
        files: list[BridgeMediaFile],
        size: int,
    ) -> BridgeMediaFile | None:
        """Read one Discord non-image attachment into local storage."""

        if len(files) >= MAX_MEDIA_FILES_PER_MESSAGE:
            await message.channel.send(
                f"⚠️ Skipped attachment: maximum {MAX_MEDIA_FILES_PER_MESSAGE} files per message."
            )
            return None
        if size > MAX_MEDIA_BYTES:
            await message.channel.send(
                f"⚠️ Skipped attachment: file is larger than {MAX_MEDIA_BYTES // (1024 * 1024)} MB"
            )
            return None
        try:
            data = await download_with_media_policy(
                attachment.read,
                platform="discord",
                url=getattr(attachment, "url", None)
                or getattr(attachment, "proxy_url", None),
            )
            return store_bridge_media_file(
                session_id=session_id,
                data=data,
                filename=filename,
                mime_type=effective_type,
            )
        except ValueError as exc:
            await message.channel.send(f"⚠️ Skipped attachment: {exc}")
        except Exception:
            logger.exception("Failed to read Discord media attachment")
            await message.channel.send("⚠️ Failed to read an attachment.")
        return None

    async def send_requested_output_attachments(
        self,
        session_id: str,
        *,
        metadata: dict | None = None,
    ) -> None:
        """Upload output attachments requested by runner metadata."""

        if not self._get_client():
            return

        attachments = attachments_from_metadata(metadata)
        if not attachments:
            return

        thread = await self._thread_for_session(
            session_id,
            fetch_failure_log="Failed to fetch Discord thread for output attachments",
        )
        if thread is None:
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
                        str(attachment.path),
                        filename=attachment.filename,
                        description=attachment.caption,
                    )
                )
            except Exception:
                logger.exception(
                    "Failed to prepare Discord output attachment",
                    session_id=session_id,
                    attachment_path=str(attachment.path),
                )
                failures.append(attachment.filename)

        if files:
            try:
                await with_bridge_send_retry(
                    "discord.output_attachments",
                    lambda: thread.send(files=files),
                )
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
                await with_bridge_send_retry(
                    "discord.attachment_failure_notice",
                    lambda: thread.send(
                        "Attachment upload failed: " + ", ".join(sorted(set(failures)))
                    ),
                )
            except Exception:
                logger.exception(
                    "Failed to send Discord attachment failure notice",
                    session_id=session_id,
                )

    async def send_error_attachment_bundle(
        self,
        session_id: str,
        metadata: dict | None = None,
    ) -> bool:
        """Upload an error diagnostic bundle to the Discord thread."""

        if not self._get_client():
            return False

        if not self._should_send_error_status(session_id):
            return True

        thread = await self._thread_for_session(
            session_id,
            fetch_failure_log="Failed to fetch Discord thread for error bundle",
        )
        if thread is None:
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
            await with_bridge_send_retry(
                "discord.error_bundle",
                lambda: thread.send(bundle.message, files=files),
            )
        except Exception:
            logger.exception(
                "Failed to send Discord error attachment bundle",
                session_id=session_id,
            )
            try:
                await with_bridge_send_retry(
                    "discord.error_fallback",
                    lambda: thread.send("❌ Status: error"),
                )
            except Exception:
                logger.exception(
                    "Failed to send Discord fallback error status",
                    session_id=session_id,
                )
        return True

    async def _thread_for_session(
        self, session_id: str, *, fetch_failure_log: str
    ) -> Any | None:
        """Resolve a Discord thread for the given session."""

        client = self._get_client()
        if client is None:
            return None

        thread_id = self._hydrate_thread_binding(session_id)
        if not thread_id:
            return None

        thread = client.get_channel(thread_id)
        if thread is None:
            try:
                thread = await client.fetch_channel(thread_id)
            except Exception:
                logger.warning(
                    fetch_failure_log,
                    session_id=session_id,
                    thread_id=thread_id,
                )
                return None
        return thread

    async def cancel_pending_error_attachment_task(self, session_id: str) -> None:
        """Cancel a pending delayed error diagnostic upload."""

        task = self._pending_error_attachment_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def schedule_error_attachment_bundle(
        self,
        session_id: str,
        metadata: dict | None = None,
    ) -> None:
        """Schedule a delayed error diagnostic upload."""

        existing = self._pending_error_attachment_tasks.pop(session_id, None)
        if existing and not existing.done():
            existing.cancel()

        async def _delayed_send() -> None:
            """Send the delayed error bundle unless a richer error arrives first."""

            try:
                await asyncio.sleep(self._error_attachment_delay_s)
                handled = await self.send_error_attachment_bundle(
                    session_id,
                    metadata=metadata,
                )
                if not handled:
                    await self._send_plain_error_status(session_id, metadata)
            except asyncio.CancelledError:
                return
            finally:
                self._pending_error_attachment_tasks.pop(session_id, None)

        self._pending_error_attachment_tasks[session_id] = asyncio.create_task(
            _delayed_send()
        )
