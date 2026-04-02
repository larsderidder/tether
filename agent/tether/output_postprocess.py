"""Helpers for final-output cleanup, attachment publishing, and STOP footers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re

from tether.models import Session

_ATTACHMENT_DIRECTIVE_RE = re.compile(
    r"^\s*PUBLISH AS ATTACH(?:E)?MENT:\s*(?P<path>.+?)\s*$",
    re.IGNORECASE,
)
_STOP_LINE_RE = re.compile(r"^\s*STOP\b", re.IGNORECASE)


@dataclass(frozen=True)
class PublishedAttachment:
    """Attachment request extracted from final output."""

    path: str
    filename: str
    title: str | None = None
    size_bytes: int | None = None

    def to_metadata(self) -> dict[str, object]:
        """Return a JSON-serializable event payload."""
        return {
            "path": self.path,
            "filename": self.filename,
            "title": self.title,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_metadata(cls, data: dict | None) -> PublishedAttachment | None:
        """Rebuild an attachment from event metadata."""
        if not isinstance(data, dict):
            return None
        path = str(data.get("path") or "").strip()
        filename = str(data.get("filename") or "").strip()
        if not path or not filename:
            return None
        title = str(data.get("title") or "").strip() or None
        size_bytes_raw = data.get("size_bytes")
        try:
            size_bytes = int(size_bytes_raw) if size_bytes_raw is not None else None
        except (TypeError, ValueError):
            size_bytes = None
        return cls(
            path=path,
            filename=filename,
            title=title,
            size_bytes=size_bytes,
        )


@dataclass(frozen=True)
class ProcessedFinalOutput:
    """Normalized final output plus resolved attachment metadata."""

    text: str
    attachments: tuple[PublishedAttachment, ...]
    warnings: tuple[str, ...]


def extract_publish_attachments(
    session: Session,
    text: str,
    *,
    max_attachments: int = 8,
    max_bytes: int = 25 * 1024 * 1024,
) -> ProcessedFinalOutput:
    """Parse final-output attachment directives and clean them from the text."""

    attachments: list[PublishedAttachment] = []
    warnings: list[str] = []
    cleaned_lines: list[str] = []

    for line in text.splitlines():
        match = _ATTACHMENT_DIRECTIVE_RE.match(line)
        if not match:
            cleaned_lines.append(line)
            continue

        raw_path = str(match.group("path") or "").strip()
        if not raw_path:
            warnings.append("Attachment directive ignored because the path is empty.")
            continue
        if len(attachments) >= max_attachments:
            warnings.append(
                f"Attachment directive ignored for {raw_path}: maximum of {max_attachments} attachments reached."
            )
            continue

        attachment, warning = _resolve_attachment(
            session,
            raw_path,
            max_bytes=max_bytes,
        )
        if attachment is not None:
            attachments.append(attachment)
        elif warning:
            warnings.append(warning)

    cleaned_text = "\n".join(cleaned_lines).strip()
    return ProcessedFinalOutput(
        text=cleaned_text,
        attachments=tuple(attachments),
        warnings=tuple(warnings),
    )


def compose_final_output(
    text: str,
    *,
    status: str,
    duration_ms: int | None,
    warnings: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Compose the final visible report text with exactly one STOP footer."""

    lines = _strip_existing_stop_line(text)
    if warnings:
        lines.extend(
            f"Attachment warning: {warning}" for warning in warnings if warning
        )
    lines.append(render_stop_footer(status, duration_ms))
    return "\n".join(line for line in lines if line).strip()


def render_stop_footer(status: str, duration_ms: int | None) -> str:
    """Render the terminal STOP footer."""

    emoji_map = {
        "success": "🛑✅",
        "error": "🛑❌",
        "stopped": "🛑⏹️",
    }
    emoji = emoji_map.get(status, emoji_map["success"])
    return f"STOP {emoji} {format_duration(duration_ms)}"


def duration_from_session(session: Session) -> int | None:
    """Best-effort fallback duration from session timestamps."""

    started_at = str(getattr(session, "started_at", "") or "").strip()
    if not started_at:
        return None
    try:
        started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))


def format_duration(duration_ms: int | None) -> str:
    """Format elapsed time for the STOP footer."""

    total_seconds = max(0, int(round((duration_ms or 0) / 1000)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _strip_existing_stop_line(text: str) -> list[str]:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _STOP_LINE_RE.match(lines[-1]):
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _resolve_attachment(
    session: Session,
    raw_path: str,
    *,
    max_bytes: int,
) -> tuple[PublishedAttachment | None, str | None]:
    base_dir = str(getattr(session, "directory", "") or "").strip()
    if not base_dir:
        return (
            None,
            f"{raw_path}: session has no working directory, so the attachment cannot be resolved.",
        )

    base_path = Path(base_dir).expanduser()
    try:
        allowed_root = base_path.resolve(strict=False)
    except OSError:
        allowed_root = base_path

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = allowed_root / candidate

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        return None, f"{raw_path}: file not found."
    except OSError as exc:
        return None, f"{raw_path}: could not resolve attachment path ({exc})."

    if resolved != allowed_root and allowed_root not in resolved.parents:
        return (
            None,
            f"{raw_path}: attachment path escapes the session directory {allowed_root}.",
        )
    if not resolved.is_file():
        return None, f"{raw_path}: attachment path is not a regular file."
    if not os.access(resolved, os.R_OK):
        return None, f"{raw_path}: attachment file is not readable."

    try:
        size_bytes = resolved.stat().st_size
    except OSError as exc:
        return None, f"{raw_path}: could not read attachment metadata ({exc})."
    if size_bytes > max_bytes:
        return (
            None,
            f"{raw_path}: attachment exceeds the {max_bytes} byte size limit.",
        )

    return (
        PublishedAttachment(
            path=str(resolved),
            filename=resolved.name,
            title=resolved.name,
            size_bytes=size_bytes,
        ),
        None,
    )
