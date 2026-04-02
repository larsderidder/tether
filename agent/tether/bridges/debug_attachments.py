"""Helpers for building debug attachment bundles for bridge error delivery."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from tether.settings import settings

_SESSION_EVENT_LIMIT_CHARS = 200_000
_LOG_TAIL_LIMIT_CHARS = 200_000
_RECENT_OUTPUT_LIMIT_CHARS = 50_000


@dataclass(frozen=True)
class DebugAttachment:
    """A text attachment that can be uploaded by a bridge."""

    filename: str
    content: str
    title: str | None = None


@dataclass(frozen=True)
class DebugAttachmentBundle:
    """A short human-facing message plus the attachments to upload."""

    message: str
    attachments: tuple[DebugAttachment, ...]


def build_error_debug_bundle(
    session_id: str,
    *,
    metadata: dict | None = None,
) -> DebugAttachmentBundle:
    """Build a diagnostic bundle for bridge-side error delivery."""
    from tether.store import store

    session = store.get_session(session_id)
    error_message = str(
        (metadata or {}).get("message") or "No explicit error message provided."
    )
    session_dir = Path(settings.data_dir()) / "sessions" / session_id
    events_path = session_dir / "events.jsonl"

    event_log_text = _read_text_tail(events_path, _SESSION_EVENT_LIMIT_CHARS)
    log_path = _resolve_log_path()
    log_tail_text = _read_text_tail(log_path, _LOG_TAIL_LIMIT_CHARS)
    recent_output = "\n".join(store.get_recent_output(session_id))
    if len(recent_output) > _RECENT_OUTPUT_LIMIT_CHARS:
        recent_output = (
            f"[truncated to last {_RECENT_OUTPUT_LIMIT_CHARS} chars]\n"
            + recent_output[-_RECENT_OUTPUT_LIMIT_CHARS:]
        )

    summary = {
        "session_id": session_id,
        "error_message": error_message,
        "metadata": metadata or {},
        "session": {
            "name": getattr(session, "name", None),
            "state": getattr(getattr(session, "state", None), "value", None),
            "runner_type": getattr(session, "runner_type", None),
            "runner_header": getattr(session, "runner_header", None),
            "runner_session_id": getattr(session, "runner_session_id", None),
            "directory": getattr(session, "directory", None),
            "platform": getattr(session, "platform", None),
            "platform_thread_id": getattr(session, "platform_thread_id", None),
            "created_at": getattr(session, "created_at", None),
            "started_at": getattr(session, "started_at", None),
            "ended_at": getattr(session, "ended_at", None),
            "last_activity_at": getattr(session, "last_activity_at", None),
            "exit_code": getattr(session, "exit_code", None),
        },
        "paths": {
            "session_events": str(events_path),
            "tether_log_file": str(log_path) if log_path else None,
        },
    }

    attachments = [
        DebugAttachment(
            filename="error-summary.txt",
            title="Error summary",
            content=json.dumps(summary, indent=2, ensure_ascii=True, default=str)
            + "\n",
        ),
        DebugAttachment(
            filename="session-events.jsonl",
            title="Session events",
            content=event_log_text or "No session event log found.\n",
        ),
        DebugAttachment(
            filename="recent-output.txt",
            title="Recent output",
            content=recent_output or "No recent output captured.\n",
        ),
        DebugAttachment(
            filename="backtraces.txt",
            title="Backtraces",
            content=_build_traceback_report(
                error_message=error_message,
                event_log_text=event_log_text,
                log_tail_text=log_tail_text,
            ),
        ),
    ]

    if log_path:
        attachments.append(
            DebugAttachment(
                filename="tether-log-tail.txt",
                title="Tether log tail",
                content=log_tail_text
                or f"Configured Tether log file was empty or unreadable: {log_path}\n",
            )
        )

    return DebugAttachmentBundle(
        message=(
            f"Error diagnostics attached for `{session_id}`.\n"
            f"Message: {error_message}"
        ),
        attachments=tuple(attachments),
    )


def _resolve_log_path() -> Path | None:
    raw = settings.log_file().strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _read_text_tail(path: Path | None, limit_chars: int) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= limit_chars:
        return text
    return (
        f"[truncated to last {limit_chars} chars from {path}]\n" + text[-limit_chars:]
    )


def _build_traceback_report(
    *,
    error_message: str,
    event_log_text: str,
    log_tail_text: str,
) -> str:
    sections: list[str] = []

    if error_message.strip():
        sections.append("# Error metadata\n" + error_message.strip())

    for label, text in (
        ("Session events", event_log_text),
        ("Tether log tail", log_tail_text),
    ):
        blocks = _extract_traceback_blocks(text)
        if blocks:
            sections.append(f"# {label}\n" + "\n\n".join(blocks))

    if not sections:
        return (
            "No traceback or backtrace material was found in error metadata, "
            "session events, or the configured Tether log file.\n"
        )
    return "\n\n".join(sections).strip() + "\n"


def _extract_traceback_blocks(text: str) -> list[str]:
    if not text:
        return []
    lines = text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        lowered = lines[i].lower()
        if "traceback" not in lowered and "backtrace" not in lowered:
            i += 1
            continue
        start = max(0, i - 2)
        end = min(len(lines), i + 28)
        block = "\n".join(lines[start:end]).strip()
        if block:
            blocks.append(block)
        i = end
    return blocks
