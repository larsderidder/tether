"""SSE event emission helpers."""

from __future__ import annotations

import structlog

from tether.api.state import now
from tether.models import Session
from tether.store import store

logger = structlog.get_logger("tether.runner")


async def emit_header(
    session: Session,
    *,
    title: str,
    model: str | None = None,
    provider: str | None = None,
    sandbox: str | None = None,
    approval: str | None = None,
) -> None:
    """Emit a structured header event with session configuration.

    Args:
        session: Session to report.
        title: Runner title/version (e.g. "Claude Code 1.0.0").
        model: Model identifier.
        provider: Provider name (e.g. "Anthropic", "OpenAI").
        sandbox: Sandbox mode.
        approval: Approval policy.
    """
    data = {"title": title, "session_id": session.id}
    if model:
        data["model"] = model
    if provider:
        data["provider"] = provider
    if sandbox:
        data["sandbox"] = sandbox
    if approval:
        data["approval"] = approval

    await store.emit(
        session.id,
        {
            "session_id": session.id,
            "ts": now(),
            "seq": store.next_seq(session.id),
            "type": "header",
            "data": data,
        },
    )


async def emit_state(session: Session) -> None:
    """Emit a session_state event to SSE listeners.

    Args:
        session: Session to report.
    """
    await store.emit(
        session.id,
        {
            "session_id": session.id,
            "ts": now(),
            "seq": store.next_seq(session.id),
            "type": "session_state",
            "data": {"state": session.state},
        },
    )


async def emit_output(
    session: Session, text: str, *, kind: str, is_final: bool | None
) -> None:
    """Emit output text if it is not a recent duplicate.

    Args:
        session: Session that produced the output.
        text: Raw output text.
        kind: Output kind ("step", "final", or "header").
        is_final: Optional explicit finality flag.
    """
    if not store.should_emit_output(session.id, text):
        return
    logger.info("Emitting output", session_id=session.id, text=text[:200])
    await store.emit(
        session.id,
        {
            "session_id": session.id,
            "ts": now(),
            "seq": store.next_seq(session.id),
            "type": "output",
            "data": {
                "stream": "combined",
                "text": text,
                "kind": kind,
                "final": is_final,
            },
        },
    )


async def emit_error(session: Session, code: str, message: str) -> None:
    """Emit an error event to SSE listeners.

    Args:
        session: Session that encountered the error.
        code: Error code string.
        message: Human-readable error message.
    """
    await store.emit(
        session.id,
        {
            "session_id": session.id,
            "ts": now(),
            "seq": store.next_seq(session.id),
            "type": "error",
            "data": {"code": code, "message": message},
        },
    )


async def emit_metadata(session: Session, key: str, value: object, raw: str) -> None:
    """Emit a metadata event to SSE listeners.

    Args:
        session: Session associated with the metadata.
        key: Metadata key identifier.
        value: Parsed metadata value.
        raw: Raw metadata string.
    """
    await store.emit(
        session.id,
        {
            "session_id": session.id,
            "ts": now(),
            "seq": store.next_seq(session.id),
            "type": "metadata",
            "data": {"key": key, "value": value, "raw": raw},
        },
    )


async def emit_heartbeat(session: Session, elapsed_s: float, done: bool) -> None:
    """Emit a heartbeat event for long-running sessions.

    Args:
        session: Session associated with the heartbeat.
        elapsed_s: Seconds elapsed since start.
        done: Whether the session has finished.
    """
    await store.emit(
        session.id,
        {
            "session_id": session.id,
            "ts": now(),
            "seq": store.next_seq(session.id),
            "type": "heartbeat",
            "data": {"elapsed_s": elapsed_s, "done": done},
        },
    )


async def emit_user_input(session: Session, text: str) -> None:
    """Emit a user_input event when the user sends a message.

    Args:
        session: Session receiving the input.
        text: The user's input text.
    """
    await store.emit(
        session.id,
        {
            "session_id": session.id,
            "ts": now(),
            "seq": store.next_seq(session.id),
            "type": "user_input",
            "data": {"text": text},
        },
    )


async def emit_input_required(session: Session, last_output: str | None = None) -> None:
    """Emit an input_required event when agent needs user input.

    Args:
        session: Session awaiting input.
        last_output: Optional recent output to include for context.
    """
    truncated = False
    if last_output and len(last_output) > 500:
        last_output = last_output[:500] + "..."
        truncated = True

    await store.emit(
        session.id,
        {
            "session_id": session.id,
            "ts": now(),
            "seq": store.next_seq(session.id),
            "type": "input_required",
            "data": {
                "session_name": session.name,
                "last_output": last_output,
                "truncated": truncated,
            },
        },
    )
