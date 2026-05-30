"""Safe inbound non-image media storage for bridge input."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from uuid import uuid4

from tether.bridges.image_io import sanitize_filename as sanitize_image_filename
from tether.settings import settings

MAX_MEDIA_BYTES = 25 * 1024 * 1024
MAX_MEDIA_FILES_PER_MESSAGE = 4
SUPPORTED_MEDIA_PREFIXES = ("audio/", "video/", "text/")
SUPPORTED_MEDIA_TYPES = {
    "application/pdf",
    "application/json",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/zip",
    "text/csv",
    "text/markdown",
    "text/plain",
}


@dataclass(frozen=True)
class BridgeMediaFile:
    """A downloaded bridge media file that agents can inspect locally."""

    path: str
    filename: str
    mime_type: str
    size: int


def supported_media_type(mime_type: str | None) -> bool:
    """Return true when an inbound non-image attachment is accepted."""

    base_type = (mime_type or "").split(";", 1)[0].strip().lower()
    if not base_type or base_type.startswith("image/"):
        return False
    return base_type in SUPPORTED_MEDIA_TYPES or base_type.startswith(
        SUPPORTED_MEDIA_PREFIXES
    )


def sanitize_media_filename(filename: str | None, *, default: str = "attachment") -> str:
    """Return a harmless filename for local media storage."""

    image_safe = sanitize_image_filename(filename, mime_type="image/png")
    if image_safe:
        stem = Path(image_safe).stem
        suffix = Path(filename or "").suffix[:20]
        safe = f"{stem}{suffix}" if suffix else stem
    else:
        safe = default
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", safe).strip(" ._")
    if not safe:
        safe = default
    return safe[:120]


def store_bridge_media_file(
    *,
    session_id: str,
    data: bytes,
    filename: str | None,
    mime_type: str | None,
) -> BridgeMediaFile:
    """Persist a supported non-image bridge file under Tether's data dir."""

    if not data:
        raise ValueError("attachment is empty")
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError(f"attachment is larger than {MAX_MEDIA_BYTES // (1024 * 1024)} MB")
    base_type = (mime_type or "").split(";", 1)[0].strip().lower()
    if not supported_media_type(base_type):
        raise ValueError("attachment type is not supported")

    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)[:120] or "session"
    safe_filename = sanitize_media_filename(filename)
    directory = Path(settings.data_dir()) / "bridge-media" / safe_session
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{uuid4().hex}-{safe_filename}"
    path.write_bytes(data)
    return BridgeMediaFile(
        path=str(path),
        filename=safe_filename,
        mime_type=base_type,
        size=len(data),
    )


def append_media_file_references(text: str, files: list[BridgeMediaFile]) -> str:
    """Append local media file paths to the prompt sent to the agent."""

    if not files:
        return text
    lines = [text.strip()] if text.strip() else []
    lines.append("Attached files saved locally:")
    for file in files:
        lines.append(
            f"- {file.filename} ({file.mime_type}, {file.size} bytes): {file.path}"
        )
    return "\n".join(lines)
