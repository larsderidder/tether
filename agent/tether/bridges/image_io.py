"""Safe helpers for bridge image pass-through."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 4
SUPPORTED_IMAGE_MIME_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


@dataclass(frozen=True)
class BridgeImage:
    """Image payload accepted by a chat bridge."""

    data: str
    mime_type: str
    filename: str | None = None

    def as_api_payload(self) -> dict[str, str]:
        """Return the JSON shape expected by Tether's session input API."""

        payload = {"type": "image", "data": self.data, "mimeType": self.mime_type}
        if self.filename:
            payload["filename"] = self.filename
        return payload


def detect_image_mime_type(data: bytes) -> str | None:
    """Return a supported image MIME type by sniffing file bytes."""

    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def make_bridge_image(
    data: bytes,
    *,
    declared_mime_type: str | None = None,
    filename: str | None = None,
) -> BridgeImage:
    """Validate and encode an inbound bridge image."""

    if not data:
        raise ValueError("image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB")

    sniffed_mime_type = detect_image_mime_type(data)
    if sniffed_mime_type is None:
        raise ValueError("attachment is not a supported image")

    if declared_mime_type:
        base_declared_mime_type = declared_mime_type.split(";", 1)[0].strip().lower()
        if (
            base_declared_mime_type.startswith("image/")
            and base_declared_mime_type in SUPPORTED_IMAGE_MIME_TYPES
            and base_declared_mime_type != sniffed_mime_type
        ):
            raise ValueError("image MIME type does not match its bytes")

    safe_filename = sanitize_filename(filename, mime_type=sniffed_mime_type)
    return BridgeImage(
        data=base64.b64encode(data).decode("ascii"),
        mime_type=sniffed_mime_type,
        filename=safe_filename,
    )


def sanitize_filename(filename: str | None, *, mime_type: str) -> str | None:
    """Return a harmless display filename for an image attachment."""

    if not filename:
        return None
    name = Path(filename).name.strip().replace("\x00", "")
    if not name or name in {".", ".."}:
        return None
    suffix = "." + SUPPORTED_IMAGE_MIME_TYPES[mime_type]
    if not Path(name).suffix:
        name = f"{name}{suffix}"
    return name[:120]


def images_from_payload(value: Any) -> list[dict[str, str]]:
    """Normalize API image payloads to pi RPC image content."""

    if not isinstance(value, list):
        return []

    images: list[dict[str, str]] = []
    for item in value[:MAX_IMAGES_PER_MESSAGE]:
        if not isinstance(item, dict):
            continue
        data = item.get("data")
        mime_type = item.get("mimeType") or item.get("mime_type")
        if not isinstance(data, str) or not isinstance(mime_type, str):
            continue
        base_mime_type = mime_type.split(";", 1)[0].strip().lower()
        if base_mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
            continue
        try:
            decoded = base64.b64decode(data, validate=True)
        except Exception:
            continue
        if len(decoded) > MAX_IMAGE_BYTES:
            continue
        if detect_image_mime_type(decoded) != base_mime_type:
            continue
        images.append({"type": "image", "data": data, "mimeType": base_mime_type})
    return images
