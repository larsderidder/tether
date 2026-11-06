"""Shared external session sync helpers."""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
import uuid
from datetime import datetime, timedelta, timezone

import structlog

from tether.api.emit import emit_history_message
from tether.api.errors import raise_http_error
from tether.api.schemas import SyncResult
from tether.bridges.retry import bridge_delivery_lease, send_bridge_delivery
from tether.discovery import get_external_session_detail
from tether.models import ExternalRunnerType, ExternalSessionMessage, Session
from tether.settings import settings
from tether.store import store

logger = structlog.get_logger(__name__)

_REPLAY_MESSAGES = 10
_REPLAY_CONTENT_LIMIT = 300
_REPLAY_THINKING_LIMIT = 150
_REPLAY_TOTAL_LIMIT = 1900
_FORCE_SYNC_REPLAY_LIMIT = 75
_BASELINE_RECOVERY_REPLAY_LIMIT = 25
_VERBOSITIES = {"none", "minimal", "medium", "high"}
_HISTORY_BUFFERS: dict[str, list[tuple[str | None, str]]] = {}
_HISTORY_FLUSH_TASKS: dict[str, asyncio.Task] = {}


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


def recent_attach_history(
    messages: list[ExternalSessionMessage],
) -> tuple[list[ExternalSessionMessage], int]:
    """Return the latest messages within both configured Telegram replay limits."""
    max_messages = settings.telegram_attach_history_max_messages()
    minutes = settings.telegram_attach_history_minutes()
    if not max_messages or not minutes:
        return [], len(messages)

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    start = len(messages)
    count_start = max(0, len(messages) - max_messages)
    for index in range(len(messages) - 1, count_start - 1, -1):
        timestamp = _parse_timestamp(messages[index].timestamp)
        # Unknown timestamps must not bypass the age limit.
        if timestamp is None or timestamp < cutoff:
            break
        start = index
    return messages[start:], start


async def queue_recent_attach_history(
    session: Session, messages: list[ExternalSessionMessage]
) -> None:
    """Queue bounded attachment history for background delivery without delaying attach."""
    recent, base_index = recent_attach_history(messages)
    for index, message in enumerate(recent):
        is_final = message.role == "assistant" and (
            index + 1 == len(recent) or recent[index + 1].role == "user"
        )
        await relay_history_message_to_bridge(
            session=session,
            bridge=None,
            role=message.role,
            content=message.content,
            thinking=message.thinking,
            is_final=is_final,
            message_index=base_index + index,
            replay_id=session.platform_thread_id,
        )
    await _flush_history_buffer(session, None)


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


def _live_user_input_counts(session_id: str) -> Counter[str]:
    """Count user prompts already emitted live by Tether."""
    counts: Counter[str] = Counter()
    for event in store.read_event_log(session_id, since_seq=0):
        if event.get("type") != "user_input":
            continue
        data = event.get("data") or {}
        if data.get("is_history"):
            continue
        normalized = " ".join(str(data.get("text") or "").split())
        if normalized:
            counts[normalized] += 1
    return counts


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


def _history_verbosity(session) -> str:
    """Return the effective bridge verbosity for imported history."""
    verbosity = str(
        getattr(session, "bridge_verbosity", None) or settings.bridge_verbosity()
    ).lower()
    if verbosity not in _VERBOSITIES:
        return settings.bridge_verbosity()
    return verbosity


def _history_buffer_max_seconds(session) -> float | None:
    """Return the effective bridge buffer delay for imported history."""
    buffer_max_seconds = getattr(session, "bridge_buffer_max_seconds", None)
    if buffer_max_seconds is None:
        return settings.bridge_buffer_max_seconds()
    return buffer_max_seconds


def _should_relay_history_chunk(session, metadata: dict) -> bool:
    """Return true when an imported bridge chunk passes output policy."""
    if metadata.get("role") != "assistant":
        return True
    if metadata.get("final") or metadata.get("is_final"):
        return True
    return _history_verbosity(session) != "none"


def _history_delivery_key(session, delivery_key: str | None) -> str:
    """Use source identity when known, otherwise assign a fresh delivery key."""
    platform = str(getattr(session, "platform", None) or "none")
    if delivery_key:
        return f"{session.id}:{platform}:{delivery_key}"
    return f"{session.id}:{platform}:history:{uuid.uuid4().hex}"


async def _drain_history_outbox(session, bridge: object | None) -> bool:
    """Send pending external history deliveries in persisted order."""
    if bridge is None or not session.platform:
        return True
    while True:
        deliveries = store.claim_pending_bridge_deliveries(
            session.id, session.platform, limit=1
        )
        if not deliveries:
            return True
        delivery = deliveries[0]
        try:
            payload = json.loads(delivery.payload)
            if delivery.method == "on_approval_request":
                request = (
                    payload.get("request", {}) if isinstance(payload, dict) else {}
                )
                request_id = str(request.get("request_id") or "")
                if not request_id or store.permission_request_stale(
                    session.id, request_id
                ):
                    store.cancel_bridge_delivery(delivery.id)
                    continue
            async with bridge_delivery_lease(store, delivery.id):
                await send_bridge_delivery(
                    bridge,
                    delivery.session_id,
                    delivery.method,
                    payload if isinstance(payload, dict) else {},
                )
                store.mark_bridge_delivery_sent(delivery.id)
        except Exception as exc:
            store.mark_bridge_delivery_failed(delivery.id, exc, retry_delay_s=0)
            logger.exception(
                "Failed to deliver external history outbox item",
                session_id=session.id,
                platform=session.platform,
                delivery_id=delivery.id,
            )
            return False


async def _flush_history_buffer(session, bridge: object | None) -> None:
    """Send buffered non-final external history to the bound bridge."""
    task = _HISTORY_FLUSH_TASKS.pop(session.id, None)
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()

    chunks = _HISTORY_BUFFERS.pop(session.id, [])
    keys = [key for key, _text in chunks if key]
    text = "\n\n".join(chunk for _key, chunk in chunks if chunk.strip()).strip()
    if not text:
        return

    delivery_key = None
    if keys:
        delivery_key = f"history-buffer:{keys[0]}:{keys[-1]}"
    await _send_history_immediately(
        session=session,
        bridge=bridge,
        text=text,
        metadata={"is_history": True, "role": "assistant", "kind": "step"},
        delivery_key=delivery_key,
    )


async def _schedule_history_flush(session, bridge: object | None) -> None:
    """Schedule a delayed flush for buffered external history."""
    delay_seconds = _history_buffer_max_seconds(session)
    if delay_seconds is None:
        return
    if delay_seconds <= 0:
        await _flush_history_buffer(session, bridge)
        return
    existing = _HISTORY_FLUSH_TASKS.get(session.id)
    if existing and not existing.done():
        return

    async def _delayed_flush() -> None:
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return
        await _flush_history_buffer(session, bridge)

    _HISTORY_FLUSH_TASKS[session.id] = asyncio.create_task(_delayed_flush())


async def _send_history_immediately(
    *,
    session,
    bridge: object | None,
    text: str,
    metadata: dict,
    delivery_key: str | None = None,
) -> None:
    """Persist one imported history chunk and attempt ordered delivery."""
    if not session.platform or not text.strip():
        return

    key = _history_delivery_key(session, delivery_key)
    store.enqueue_bridge_delivery(
        session_id=session.id,
        platform=session.platform,
        delivery_key=key,
        method="on_output",
        payload={"text": text, "metadata": metadata},
    )
    await _drain_history_outbox(session, bridge)


async def send_history_to_bridge(
    *,
    session,
    bridge: object | None,
    text: str,
    metadata: dict,
    delivery_key: str | None = None,
) -> None:
    """Send or buffer one imported history chunk to the bound bridge thread."""
    if not session.platform or not text.strip():
        return

    is_final = bool(metadata.get("final") or metadata.get("is_final"))
    if not _should_relay_history_chunk(session, metadata):
        return

    is_bufferable_history = (
        bool(metadata.get("is_history"))
        and metadata.get("role") == "assistant"
        and not is_final
        and not metadata.get("replay")
    )

    if is_final:
        await _flush_history_buffer(session, bridge)
        await _send_history_immediately(
            session=session,
            bridge=bridge,
            text=text,
            metadata=metadata,
            delivery_key=delivery_key,
        )
        return

    if not is_bufferable_history:
        await _send_history_immediately(
            session=session,
            bridge=bridge,
            text=text,
            metadata=metadata,
            delivery_key=delivery_key,
        )
        return

    _HISTORY_BUFFERS.setdefault(session.id, []).append((delivery_key, text))
    await _schedule_history_flush(session, bridge)


async def relay_history_message_to_bridge(
    *,
    session,
    bridge: object | None,
    role: str,
    content: str,
    thinking: str | None = None,
    is_final: bool = False,
    message_index: int | None = None,
    replay_id: str | None = None,
) -> None:
    """Mirror one imported external-session message into the bridge thread."""
    message_key = f"history:{message_index}"
    if replay_id is not None:
        message_key = f"attach:{replay_id}:{message_index}"
    replay_metadata = {"replay": True} if replay_id is not None else {}
    if role == "user":
        await send_history_to_bridge(
            session=session,
            bridge=bridge,
            text=format_history_user_text(content),
            metadata={"is_history": True, "role": "user", **replay_metadata},
            delivery_key=(f"{message_key}:user" if message_index is not None else None),
        )
        return

    if thinking:
        await send_history_to_bridge(
            session=session,
            bridge=bridge,
            text=thinking,
            metadata={
                "is_history": True,
                "role": "assistant",
                "kind": "step",
                "final": False,
                **replay_metadata,
            },
            delivery_key=(
                f"{message_key}:thinking" if message_index is not None else None
            ),
        )

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
                **replay_metadata,
            },
            delivery_key=(
                f"{message_key}:content" if message_index is not None else None
            ),
        )


async def replay_stored_history_to_bridge(*, session, bridge: object | None) -> None:
    """Explicitly backfill the latest 100 imported messages into a new bridge thread."""
    if bridge is None:
        return

    await _drain_history_outbox(session, bridge)

    if not session.platform:
        return
    store.bridge_replay_cursor(session.id, session.platform)
    history_events = [
        event
        for event in store.read_event_log(session.id)
        if (event.get("data") or {}).get("is_history")
        and event.get("type") in {"user_input", "output"}
    ][-100:]

    for event in history_events:
        data = event.get("data") or {}
        if event.get("type") == "user_input":
            await send_history_to_bridge(
                session=session,
                bridge=bridge,
                text=format_history_user_text(str(data.get("text") or "")),
                metadata={"is_history": True, "role": "user", "replay": True},
                delivery_key=f"history-replay:{event.get('seq')}:user",
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
                "replay": True,
            },
            delivery_key=f"history-replay:{event.get('seq')}:assistant",
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
    # The persisted cursor is an absolute transcript index, not a tail offset.
    # Providers already scan the full transcript; do not truncate that snapshot.
    detail = await asyncio.to_thread(
        get_external_session_detail,
        session_id=external_id,
        runner_type=runner_type,
        limit=sys.maxsize,
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
    elif synced_count < 0:
        turn_count = sum(1 for message in messages if message.role == "user")
        store.set_synced_message_count(session_id, len(messages), turn_count)
        logger.info(
            "Initialized persisted sync baseline without replay",
            session_id=session_id,
            source=source,
            total_messages=len(messages),
        )
        return SyncResult(synced=0, total=len(messages))
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

    history_bridge = get_bound_bridge(session)
    live_user_inputs = _live_user_input_counts(session.id)
    turn_count = sum(1 for message in messages[:base_idx] if message.role == "user")
    for i, msg in enumerate(new_messages):
        if msg.role == "user":
            turn_count += 1
            normalized_input = " ".join(str(msg.content or "").split())
            if live_user_inputs[normalized_input] > 0:
                live_user_inputs[normalized_input] -= 1
                store.set_synced_message_count(session_id, base_idx + i + 1, turn_count)
                continue

        is_final = False
        if msg.role == "assistant":
            next_idx = base_idx + i + 1
            if next_idx >= len(messages) or messages[next_idx].role == "user":
                is_final = True

        await relay_history_message_to_bridge(
            session=session,
            bridge=history_bridge,
            role=msg.role,
            content=msg.content,
            thinking=msg.thinking,
            is_final=is_final,
            message_index=base_idx + i,
        )
        await _flush_history_buffer(session, history_bridge)
        await emit_history_message(
            session,
            role=msg.role,
            content=msg.content,
            thinking=msg.thinking,
            timestamp=msg.timestamp,
            is_final=is_final,
            is_history=True,
        )
        store.set_synced_message_count(session_id, base_idx + i + 1, turn_count)
    await _flush_history_buffer(session, history_bridge)

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
