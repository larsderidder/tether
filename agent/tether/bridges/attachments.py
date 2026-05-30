"""Shared outbound attachment policy for chat bridges."""

from __future__ import annotations

from dataclasses import dataclass
import mimetypes
from pathlib import Path
from typing import Any

from tether.bridges.image_io import detect_image_mime_type
from tether.output_postprocess import PublishedAttachment


@dataclass(slots=True)
class OutboundAttachment:
    """Attachment with bridge-send policy already resolved."""

    published: PublishedAttachment
    path: Path
    media_type: str
    send_as_image: bool
    size: int

    @property
    def filename(self) -> str:
        return self.published.filename

    @property
    def caption(self) -> str:
        return self.published.title or self.published.filename


def attachments_from_metadata(
    metadata: dict[str, Any] | None,
    *,
    max_count: int | None = None,
    max_bytes: int | None = None,
) -> list[OutboundAttachment]:
    """Resolve published attachments into a shared bridge-send policy."""

    attachments: list[OutboundAttachment] = []
    for item in (metadata or {}).get("attachments") or []:
        published = PublishedAttachment.from_metadata(item)
        if published is None:
            continue
        resolved = resolve_outbound_attachment(published, max_bytes=max_bytes)
        if resolved is None:
            continue
        attachments.append(resolved)
        if max_count is not None and len(attachments) >= max_count:
            break
    return attachments


def resolve_outbound_attachment(
    attachment: PublishedAttachment,
    *,
    max_bytes: int | None = None,
) -> OutboundAttachment | None:
    """Resolve one attachment, sniffing bytes before image-specific sends."""

    path = Path(attachment.path)
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    if max_bytes is not None and stat.st_size > max_bytes:
        return None

    media_type = mimetypes.guess_type(attachment.filename)[0] or ""
    sniffed_image_type = ""
    try:
        with path.open("rb") as handle:
            sniffed_image_type = detect_image_mime_type(handle.read(64)) or ""
    except OSError:
        return None

    return OutboundAttachment(
        published=attachment,
        path=path,
        media_type=media_type,
        send_as_image=bool(sniffed_image_type and media_type.startswith("image/")),
        size=stat.st_size,
    )
