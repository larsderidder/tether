"""Shared external session sync helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from tether.api.emit import emit_history_message
from tether.api.errors import raise_http_error
from tether.api.schemas import SyncResult
from tether.discovery import get_external_session_detail
from tether.models import ExternalRunnerType
from tether.store import store

logger = structlog.get_logger(__name__)

_REPLAY_MESSAGES = 10
_REPLAY_CONTENT_LIMIT = 300
_REPLAY_THINKING_LIMIT = 150
_REPLAY_TOTAL_LIMIT = 1900
_FORCE_SYNC_REPLAY_LIMIT = 75
_BASELINE_RECOVERY_REPLAY_LIMIT = 25


def _parse_timestamp(value: object) -> datetime | None:
    """Parse a provider timestamp into an aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _messages_within_lookback(
    messages: list,
    *,
    lookback_seconds: float,
) -> tuple[list, int]:
    """Return messages from the recent lookback window and their base index."""
    if lookback_seconds <= 0:
        return [], len(messages)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=lookback_seconds)
    for index, message in enumerate(messages):
        timestamp = _parse_timestamp(getattr(message, "timestamp", None))
        if timestamp and timestamp >= cutoff:
            return messages[index:], index
    return [], len(messages)


def _event_log_recovery_timestamp(session_id: str) -> datetime | None:
    """Return the latest transcript timestamp when live output exists."""
    latest: datetime | None = None
    has_live_transcript = False
    for event in store.read_event_log(session_id, since_seq=0):
        if event.get("type") not in {"user_input", "output", "output_final"}:
            continue
        if not (event.get("data") or {}).get("is_history"):
            has_live_transcript = True
        timestamp = _parse_timestamp(event.get("ts"))
        if timestamp and (latest is None or timestamp > latest):
            latest = timestamp
    if not has_live_transcript:
        return None
    return latest


def _messages_after_timestamp(messages: list, after: datetime) -> tuple[list, int]:
    """Return messages whose provider timestamp is newer than after."""
    for index, message in enumerate(messages):
        timestamp = _parse_timestamp(getattr(message, "timestamp", None))
        if timestamp and timestamp > after:
            return messages[index:], index
    return [], len(messages)


def external_runner_type_for_session(session) -> ExternalRunnerType:
    """Infer the external runner type for an attached Tether session."""
    runner_type = str(getattr(session, "runner_type", "") or "").strip().lower()
    adapter = str(getattr(session, "adapter", "") or "").strip().lower()

    if runner_type == "pi" or adapter == "pi_rpc":
        return ExternalRunnerType.PI
    if runner_type == "codex" or adapter == "codex_sdk_sidecar":
        return ExternalRunnerType.CODEX
    if runner_type == "opencode" or adapter == "opencode":
        return ExternalRunnerType.OPENCODE
    return ExternalRunnerType.CLAUDE_CODE


def get_pi_metadata(external_id: str) -> dict | None:
    """Fetch model and thinking level for a pi session."""
    try:
        from agent_sessions.providers.pi import (
            _find_session_file,
            get_pi_session_model,
            get_pi_session_thinking_level,
        )

        session_file = _find_session_file(external_id)
        if not session_file:
            return None
        model_info = get_pi_session_model(session_file)
        thinking_level = get_pi_session_thinking_level(session_file)
        result: dict = {}
        if model_info:
            result["model"] = model_info[1]
        if thinking_level:
            result["thinking_level"] = thinking_level
        return result or None
    except Exception:
        return None


def format_replay(messages: list, metadata: dict | None = None) -> str | None:
    """Format the last messages as a compact history replay string."""
    recent = (
        messages[-_REPLAY_MESSAGES:] if len(messages) > _REPLAY_MESSAGES else messages
    )
    if not recent:
        return None

    header = f"Recent history (last {len(recent)} messages)"
    if metadata:
        parts = []
        if metadata.get("model"):
            parts.append(metadata["model"])
        if metadata.get("thinking_level"):
            parts.append(f"thinking: {metadata['thinking_level']}")
        if parts:
            header += f" ({', '.join(parts)})"
    lines: list[str] = [header + ":\n"]
    for i, msg in enumerate(recent, 1):
        role = (msg.role if hasattr(msg, "role") else msg.get("role", "")).lower()
        prefix = "👤" if role == "user" else ("🤖" if role == "assistant" else "?")
        content = (
            msg.content if hasattr(msg, "content") else msg.get("content") or ""
        ) or ""
        thinking = (
            msg.thinking if hasattr(msg, "thinking") else msg.get("thinking") or ""
        ) or ""
        content = content.strip()
        thinking = thinking.strip()
        if content and len(content) > _REPLAY_CONTENT_LIMIT:
            content = content[:_REPLAY_CONTENT_LIMIT] + "..."
        if thinking and len(thinking) > _REPLAY_THINKING_LIMIT:
            thinking = thinking[:_REPLAY_THINKING_LIMIT] + "..."
        if content:
            lines.append(f"{i}. {prefix}: {content}")
        if thinking:
            lines.append(f"   {prefix} (thinking): {thinking}")

    text = "\n".join(lines)
    if len(text) > _REPLAY_TOTAL_LIMIT:
        text = text[: _REPLAY_TOTAL_LIMIT - 3] + "..."
    return text or None


def format_history_user_text(text: str) -> str:
    """Render imported user prompts clearly inside bridge threads."""
    if "\n" in text:
        return f"👤 User\n{text}"
    return f"👤 User: {text}"


def get_bound_bridge(session) -> object | None:
    """Return the active bridge for a platform-bound session, if available."""
    platform = session.platform
    if not platform:
        return None

    from tether.bridges.glue import bridge_manager

    bridge = bridge_manager.get_bridge(platform)
    if bridge is None:
        logger.warning(
            "Skipping history relay because bridge is unavailable",
            session_id=session.id,
            platform=platform,
        )
    return bridge


async def send_history_to_bridge(
    *,
    session,
    bridge: object | None,
    text: str,
    metadata: dict,
) -> None:
    """Send one imported history chunk to the bound bridge thread."""
    if bridge is None or not text.strip():
        return

    try:
        await bridge.on_output(session.id, text, metadata=metadata)
    except Exception:
        logger.exception(
            "Failed to relay external session history to bridge",
            session_id=session.id,
            platform=session.platform,
            metadata=metadata,
        )


async def relay_history_message_to_bridge(
    *,
    session,
    bridge: object | None,
    role: str,
    content: str,
    thinking: str | None = None,
    is_final: bool = False,
) -> None:
    """Mirror one imported external-session message into the bridge thread."""
    if role == "user":
        await send_history_to_bridge(
            session=session,
            bridge=bridge,
            text=format_history_user_text(content),
            metadata={"is_history": True, "role": "user"},
        )
        return

    if content:
        kind = "final" if is_final else "step"
        await send_history_to_bridge(
            session=session,
            bridge=bridge,
            text=content,
            metadata={
                "is_history": True,
                "role": "assistant",
                "kind": kind,
                "final": is_final,
            },
        )


async def replay_stored_history_to_bridge(*, session, bridge: object | None) -> None:
    """Replay already-emitted imported history into a newly created bridge thread."""
    if bridge is None:
        return

    history_events = [
        event
        for event in store.read_event_log(session.id, since_seq=0)
        if (event.get("data") or {}).get("is_history")
        and event.get("type") in {"user_input", "output"}
    ]

    for event in history_events:
        data = event.get("data") or {}
        if event.get("type") == "user_input":
            await send_history_to_bridge(
                session=session,
                bridge=bridge,
                text=format_history_user_text(str(data.get("text") or "")),
                metadata={"is_history": True, "role": "user"},
            )
            continue

        await send_history_to_bridge(
            session=session,
            bridge=bridge,
            text=str(data.get("text") or ""),
            metadata={
                "is_history": True,
                "role": "assistant",
                "kind": data.get("kind"),
                "final": bool(data.get("final")),
            },
        )


async def sync_external_session_delta(
    session_id: str,
    *,
    force: bool = False,
    source: str = "manual",
    initial_lookback_seconds: float | None = None,
) -> SyncResult:
    """Sync new messages from an attached external session."""
    session = store.get_session(session_id)
    if not session:
        raise_http_error("NOT_FOUND", "Session not found", 404)

    external_id = store.get_runner_session_id(session_id)
    if not external_id:
        raise_http_error(
            "INVALID_STATE",
            "Session is not attached to an external session",
            400,
        )

    runner_type = external_runner_type_for_session(session)
    detail = get_external_session_detail(
        session_id=external_id,
        runner_type=runner_type,
        limit=500,
    )
    if not detail:
        raise_http_error("NOT_FOUND", f"External session not found: {external_id}", 404)

    synced_count = store.get_synced_message_count(session_id)
    messages = detail.messages

    if force:
        replay_count = min(len(messages), _FORCE_SYNC_REPLAY_LIMIT)
        start_idx = max(0, len(messages) - replay_count)
        logger.info(
            "Force sync requested; replaying recent history window",
            session_id=session_id,
            source=source,
            synced_count=synced_count,
            total_messages=len(messages),
            replay_messages=replay_count,
        )
        new_messages = messages[start_idx:]
        base_idx = start_idx
    elif synced_count == 0 and (
        event_log_timestamp := _event_log_recovery_timestamp(session_id)
    ):
        new_messages, base_idx = _messages_after_timestamp(
            messages, event_log_timestamp
        )
        if not new_messages:
            turn_count = sum(1 for m in messages if m.role == "user")
            store.set_synced_message_count(session_id, len(messages), turn_count)
        logger.info(
            "Sync baseline missing; recovering after live event log",
            session_id=session_id,
            source=source,
            total_messages=len(messages),
            replay_messages=len(new_messages),
            event_log_timestamp=event_log_timestamp.isoformat(),
        )
    elif synced_count == 0 and initial_lookback_seconds is not None:
        new_messages, base_idx = _messages_within_lookback(
            messages,
            lookback_seconds=initial_lookback_seconds,
        )
        logger.info(
            "Sync baseline missing; replaying recent lookback window",
            session_id=session_id,
            source=source,
            total_messages=len(messages),
            replay_messages=len(new_messages),
            lookback_seconds=initial_lookback_seconds,
        )
    elif synced_count == 0:
        replay_count = min(len(messages), _BASELINE_RECOVERY_REPLAY_LIMIT)
        start_idx = max(0, len(messages) - replay_count)
        logger.info(
            "Sync baseline missing; replaying recent history window",
            session_id=session_id,
            source=source,
            total_messages=len(messages),
            replay_messages=replay_count,
        )
        new_messages = messages[start_idx:]
        base_idx = start_idx
    else:
        new_messages = messages[synced_count:]
        base_idx = synced_count

    if not new_messages:
        if synced_count == 0 and initial_lookback_seconds is not None:
            turn_count = sum(1 for m in messages if m.role == "user")
            store.set_synced_message_count(session_id, len(messages), turn_count)
        logger.debug("No new messages to sync", session_id=session_id, source=source)
        return SyncResult(synced=0, total=len(messages))

    if not force and synced_count == 0:
        existing_history = [
            event
            for event in store.read_event_log(session_id, since_seq=0)
            if (event.get("data") or {}).get("is_history")
            and event.get("type") in {"user_input", "output"}
        ]
        if len(existing_history) >= len(messages):
            turn_count = sum(1 for m in messages if m.role == "user")
            store.set_synced_message_count(session_id, len(messages), turn_count)
            return SyncResult(synced=len(new_messages), total=len(messages))

    history_bridge = get_bound_bridge(session)
    for i, msg in enumerate(new_messages):
        is_final = False
        if msg.role == "assistant":
            next_idx = base_idx + i + 1
            if next_idx >= len(messages) or messages[next_idx].role == "user":
                is_final = True

        await emit_history_message(
            session,
            role=msg.role,
            content=msg.content,
            thinking=msg.thinking,
            timestamp=msg.timestamp,
            is_final=is_final,
            is_history=True,
        )
        await relay_history_message_to_bridge(
            session=session,
            bridge=history_bridge,
            role=msg.role,
            content=msg.content,
            thinking=msg.thinking,
            is_final=is_final,
        )

    turn_count = sum(1 for m in messages if m.role == "user")
    store.set_synced_message_count(session_id, len(messages), turn_count)

    logger.info(
        "Synced external session",
        session_id=session_id,
        source=source,
        new_messages=len(new_messages),
        turn_count=turn_count,
        total_messages=len(messages),
    )

    return SyncResult(synced=len(new_messages), total=len(messages))
