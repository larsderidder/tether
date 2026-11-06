"""Bridge subscriber with Tether-local output metadata support."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import structlog
from agent_tether.base import ApprovalRequest
from agent_tether.manager import BridgeManager

from tether.bridges.rich_output_segments import parse_output_segments
from tether.bridges.retry import bridge_delivery_lease, send_bridge_delivery
from tether.bridges.turn_accumulator import BridgeTurnAccumulator, TOOL_SEGMENT_KINDS
from tether.settings import settings
from tether.store import store

logger = structlog.get_logger(__name__)

_VERBOSITIES = {"none", "minimal", "medium", "high"}
_ERROR_STATUS_COALESCE_S = 0.1


@dataclass(frozen=True)
class BridgeOutputPolicy:
    """Effective bridge output policy for one session."""

    verbosity: str
    buffer_max_seconds: float | None


class BridgeSubscriber:
    """Subscribe to store events and route them to platform bridges."""

    def __init__(
        self,
        bridge_manager: BridgeManager | None = None,
        new_subscriber=None,
        remove_subscriber=None,
        get_session: Callable[[str], Any | None] | None = None,
        outbox_retry_delay_s: float = 5.0,
    ) -> None:
        if (
            bridge_manager is None
            or new_subscriber is None
            or remove_subscriber is None
        ):
            from tether.bridges.glue import (
                _new_subscriber,
                _remove_subscriber,
                bridge_manager as _bridge_manager,
            )

            bridge_manager = bridge_manager or _bridge_manager
            new_subscriber = new_subscriber or _new_subscriber
            remove_subscriber = remove_subscriber or _remove_subscriber

        self._bridge_manager = bridge_manager
        self._new_subscriber = new_subscriber
        self._remove_subscriber = remove_subscriber
        subscriber_owner = getattr(new_subscriber, "__self__", None)
        self._get_session = get_session or getattr(
            subscriber_owner, "get_session", store.get_session
        )
        self._tasks: dict[str, asyncio.Task] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._turns = BridgeTurnAccumulator()
        self._store = subscriber_owner or store
        self._buffer_last_seq: dict[str, int] = {}
        self._output_flush_tasks: dict[str, asyncio.Task] = {}
        self._error_status_tasks: dict[str, asyncio.Task] = {}
        self._outbox_retry_delay_s = outbox_retry_delay_s
        self._last_processed_seq: dict[str, int] = {}
        self._failed_routing: set[str] = set()

    def subscribe(self, session_id: str, platform: str) -> None:
        """Start consuming store events for a session and routing to a bridge."""
        if session_id in self._tasks:
            return

        self._store.bridge_replay_cursor(session_id, platform)

        queue = self._new_subscriber(session_id)
        self._queues[session_id] = queue
        task = asyncio.create_task(self._consume(session_id, platform, queue))
        self._tasks[session_id] = task
        logger.info(
            "Bridge subscriber started", session_id=session_id, platform=platform
        )

    async def unsubscribe(
        self, session_id: str, *, platform: str | None = None
    ) -> None:
        """Stop consuming events for a session and clean up bridge state."""
        task = self._tasks.pop(session_id, None)
        self._queues.pop(session_id, None)
        if task:
            task.cancel()
            logger.info("Bridge subscriber stopped", session_id=session_id)
        output_task = self._output_flush_tasks.pop(session_id, None)
        if output_task and not output_task.done():
            output_task.cancel()
        await self._cancel_error_status(session_id)
        self._last_processed_seq.pop(session_id, None)

        if platform:
            bridge = self._bridge_manager.get_bridge(platform)
            if bridge:
                await self._flush_output(session_id, platform, bridge)
                await bridge.on_session_removed(session_id)
        self._turns.remove(session_id)
        self._failed_routing.discard(session_id)

    def _buffer_output(
        self,
        session_id: str,
        text: str,
        bridge_segments: list[dict[str, str]] | None = None,
        *,
        event_seq: int | None = None,
    ) -> None:
        """Add text and optional structured bridge segments to the output buffer."""
        self._turns.buffer_stream(session_id, text, bridge_segments)
        if event_seq is not None:
            self._buffer_last_seq[session_id] = event_seq

    @staticmethod
    def _event_seq(event: dict) -> int | None:
        """Return the integer event sequence when present."""
        try:
            seq = int(event.get("seq") or 0)
        except (TypeError, ValueError):
            return None
        return seq or None

    def _delivery_key(
        self, session_id: str, platform: str, kind: str, seq: int | None
    ) -> str:
        """Build a stable outbox key for idempotent bridge delivery."""
        if seq is not None:
            return f"{session_id}:{platform}:{kind}:{seq}"
        return f"{session_id}:{platform}:{kind}:no-seq-{uuid.uuid4().hex}"

    @staticmethod
    def _approval_request_payload(request: ApprovalRequest) -> dict:
        """Convert an approval request to JSON-safe outbox payload data."""
        return {
            "kind": request.kind,
            "request_id": request.request_id,
            "title": request.title,
            "description": request.description,
            "options": list(request.options),
        }

    def _delivery_is_resolved_permission(self, delivery: object, payload: dict) -> bool:
        """Return true when a pending approval prompt has already been answered."""
        if getattr(delivery, "method", None) != "on_approval_request":
            return False
        request = payload.get("request") if isinstance(payload, dict) else None
        if not isinstance(request, dict):
            return False
        request_id = str(request.get("request_id") or "")
        if not request_id:
            return False
        return self._store.permission_request_stale(
            str(delivery.session_id), request_id
        )

    async def _deliver_outbox(self, delivery: object, bridge: object) -> bool:
        """Attempt one bridge delivery without bypassing FIFO order."""
        claimed = self._store.claim_bridge_delivery(str(delivery.id))
        if claimed is None:
            return False
        return await self._deliver_claimed_outbox(claimed, bridge)

    async def _enqueue_and_deliver(
        self,
        *,
        session_id: str,
        platform: str,
        bridge: object,
        delivery_key: str,
        method: str,
        payload: dict,
        event_seq: int | None = None,
    ) -> bool:
        """Persist a bridge delivery, then attempt to send it."""
        try:
            delivery = self._store.enqueue_bridge_delivery(
                session_id=session_id,
                platform=platform,
                delivery_key=delivery_key,
                method=method,
                payload=payload,
                event_seq=event_seq,
            )
        except Exception:
            self._failed_routing.add(session_id)
            raise
        return await self._deliver_outbox(delivery, bridge)

    async def _drain_pending_deliveries(
        self, session_id: str, platform: str, bridge: object
    ) -> None:
        """Retry pending deliveries for this bridge binding in outbox order."""
        while True:
            deliveries = self._store.claim_pending_bridge_deliveries(
                session_id, platform, limit=1
            )
            if not deliveries:
                return
            sent = await self._deliver_claimed_outbox(deliveries[0], bridge)
            if not sent:
                return

    async def _deliver_claimed_outbox(self, delivery: object, bridge: object) -> bool:
        """Send an already claimed delivery row."""
        if getattr(delivery, "status", None) in {"sent", "cancelled"}:
            return True
        try:
            payload = json.loads(str(delivery.payload or "{}"))
            payload_dict = payload if isinstance(payload, dict) else {}
            if self._delivery_is_resolved_permission(delivery, payload_dict):
                self._store.cancel_bridge_delivery(str(delivery.id))
                return True
            async with bridge_delivery_lease(self._store, str(delivery.id)):
                await send_bridge_delivery(
                    bridge,
                    str(delivery.session_id),
                    str(delivery.method),
                    payload_dict,
                )
                self._store.mark_bridge_delivery_sent(str(delivery.id))
        except Exception as exc:
            self._store.mark_bridge_delivery_failed(
                str(delivery.id), exc, retry_delay_s=self._outbox_retry_delay_s
            )
            logger.exception(
                "Failed to deliver bridge outbox item",
                session_id=getattr(delivery, "session_id", None),
                platform=getattr(delivery, "platform", None),
                delivery_id=getattr(delivery, "id", None),
                method=getattr(delivery, "method", None),
            )
            return False
        return True

    def _policy(self, session_id: str) -> BridgeOutputPolicy:
        """Return the effective bridge output policy for a session."""
        session = self._get_session(session_id)
        verbosity = str(
            getattr(session, "bridge_verbosity", None) or settings.bridge_verbosity()
        ).lower()
        if verbosity not in _VERBOSITIES:
            verbosity = settings.bridge_verbosity()
        buffer_max_seconds = getattr(session, "bridge_buffer_max_seconds", None)
        if buffer_max_seconds is None:
            buffer_max_seconds = settings.bridge_buffer_max_seconds()
        return BridgeOutputPolicy(
            verbosity=verbosity, buffer_max_seconds=buffer_max_seconds
        )

    @staticmethod
    def _segments_from_output(
        text: str, bridge_segments: list[dict[str, str]] | None
    ) -> list[dict[str, str]]:
        """Return structured segments from metadata or legacy marker text."""
        if bridge_segments:
            return [dict(segment) for segment in bridge_segments]
        segments = []
        for segment in parse_output_segments(text):
            item = {"kind": segment.kind, "text": segment.text}
            if segment.label:
                item["label"] = segment.label
            segments.append(item)
        return segments

    @staticmethod
    def _filter_segments_for_policy(
        segments: list[dict[str, str]], policy: BridgeOutputPolicy
    ) -> list[dict[str, str]]:
        """Drop or redact non-final segments according to bridge verbosity."""
        if policy.verbosity == "none":
            return [
                dict(segment)
                for segment in segments
                if str(segment.get("kind") or "") == "warning"
            ]
        allowed = {"assistant", "thinking", "warning"}
        if policy.verbosity in {"medium", "high"}:
            allowed.add("tool_call")
        if policy.verbosity == "high":
            allowed.update(
                {
                    "automation_message",
                    "error",
                    "info",
                    "result",
                    "status",
                    "tool_error",
                    "tool_output",
                    "tool_result",
                }
            )

        filtered: list[dict[str, str]] = []
        for segment in segments:
            kind = str(segment.get("kind") or "")
            if kind not in allowed:
                continue
            item = dict(segment)
            if policy.verbosity == "medium" and kind == "tool_call":
                item["text"] = ""
            filtered.append(item)
        return filtered

    @staticmethod
    def _text_from_segments(segments: list[dict[str, str]]) -> str:
        """Build a safe fallback text payload from filtered segments."""
        parts: list[str] = []
        for segment in segments:
            kind = str(segment.get("kind") or "")
            label = str(segment.get("label") or "").strip()
            text = str(segment.get("text") or "")
            if kind == "thinking":
                parts.append(f"[thinking] {text}\n")
            elif kind == "tool_call":
                parts.append(f"[tool: {label or 'tool'}]\n")
            elif kind == "tool_output":
                parts.append(f"[{label or 'tool'}] {text}\n")
            elif kind in {"result", "tool_result"}:
                parts.append(f"[result] {text}\n")
            elif kind in {"error", "tool_error"}:
                parts.append(f"[error] {text}\n")
            elif kind == "status":
                parts.append(f"[notify] {text}\n")
            elif kind == "warning":
                parts.append(f"⚠️ {text}\n")
            else:
                parts.append(text)
        return "".join(parts)

    def _filter_output_for_policy(
        self,
        text: str,
        bridge_segments: list[dict[str, str]] | None,
        policy: BridgeOutputPolicy,
    ) -> tuple[str, list[dict[str, str]] | None]:
        """Return bridge output after applying verbosity rules."""
        segments = self._segments_from_output(text, bridge_segments)
        filtered = self._filter_segments_for_policy(segments, policy)
        if filtered:
            return self._text_from_segments(filtered), filtered
        return "", None

    @staticmethod
    def _is_confirmation_wait_output(
        bridge_segments: list[dict[str, str]] | None,
    ) -> bool:
        """Return true for redundant wait notices covered by approval prompts."""

        if not bridge_segments:
            return False
        return all(
            str(segment.get("kind") or "") == "tool_output"
            and str(segment.get("text") or "").startswith("Waiting for confirmation:")
            for segment in bridge_segments
        )

    def _has_only_buffered_tool_activity(self, session_id: str) -> bool:
        """Return true when the current buffer contains only tool telemetry."""

        kinds = self._turns.buffered_segment_kinds(session_id)
        if not kinds:
            return False
        return kinds.issubset(TOOL_SEGMENT_KINDS)

    async def _flush_output(
        self,
        session_id: str,
        platform: str,
        bridge: object,
        *,
        delivery_key: str | None = None,
        event_seq: int | None = None,
        exclude_assistant: bool = False,
        assistant_only: bool = False,
    ) -> None:
        """Persist and send buffered output, optionally selecting assistant prose."""
        if assistant_only:
            flush = self._turns.flush_assistant(session_id)
        else:
            task = self._output_flush_tasks.pop(session_id, None)
            if task and not task.done():
                task.cancel()
            flush = self._turns.flush_stream(session_id)
        if not flush:
            return

        text, metadata = flush.text, flush.metadata
        if exclude_assistant and metadata:
            segments = [
                segment
                for segment in metadata.get("bridge_segments", [])
                if segment.get("kind") != "assistant"
            ]
            if not segments:
                return
            text = self._text_from_segments(segments)
            metadata = {**metadata, "bridge_segments": segments}

        seq = event_seq or self._buffer_last_seq.get(session_id)
        key = delivery_key or self._delivery_key(session_id, platform, "flush", seq)
        await self._enqueue_and_deliver(
            session_id=session_id,
            platform=platform,
            bridge=bridge,
            delivery_key=key,
            method="on_output",
            payload={"text": text, "metadata": metadata},
            event_seq=seq,
        )
        if delivery_key is None and event_seq is None and seq is not None:
            self._advance_replay_cursor(session_id, platform, seq)

    async def _flush_tool_activity(
        self,
        session_id: str,
        platform: str,
        bridge: object,
        *,
        delivery_key: str | None = None,
        event_seq: int | None = None,
    ) -> None:
        """Persist and send buffered tool telemetry as one bridge message."""
        flush = self._turns.flush_tool_activity(session_id)
        if not flush:
            return

        seq = event_seq or self._buffer_last_seq.get(session_id)
        key = delivery_key or self._delivery_key(
            session_id, platform, "tool-flush", seq
        )
        await self._enqueue_and_deliver(
            session_id=session_id,
            platform=platform,
            bridge=bridge,
            delivery_key=key,
            method="on_output",
            payload={"text": flush.text, "metadata": flush.metadata},
            event_seq=seq,
        )

    async def _cancel_error_status(self, session_id: str) -> None:
        """Cancel a generic error status that is waiting for richer metadata."""
        task = self._error_status_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _schedule_error_status(
        self,
        session_id: str,
        platform: str,
        bridge: object,
        event_seq: int | None = None,
    ) -> None:
        """Delay a generic error so the following detailed event can replace it."""
        existing = self._error_status_tasks.get(session_id)
        if existing and not existing.done():
            return

        async def _delayed_status() -> None:
            try:
                await asyncio.sleep(_ERROR_STATUS_COALESCE_S)
                await self._enqueue_and_deliver(
                    session_id=session_id,
                    platform=platform,
                    bridge=bridge,
                    delivery_key=self._delivery_key(
                        session_id, platform, "status-error", event_seq
                    ),
                    method="on_status_change",
                    payload={"status": "error", "metadata": None},
                    event_seq=event_seq,
                )
                if event_seq is not None:
                    self._advance_replay_cursor(session_id, platform, event_seq)
            except asyncio.CancelledError:
                return
            finally:
                current = self._error_status_tasks.get(session_id)
                if current is asyncio.current_task():
                    self._error_status_tasks.pop(session_id, None)

        self._error_status_tasks[session_id] = asyncio.create_task(_delayed_status())

    async def _schedule_flush(
        self,
        session_id: str,
        platform: str,
        bridge: object,
        delay_seconds: float | None,
    ) -> None:
        """Schedule a delayed flush of buffered output when policy allows it."""
        if delay_seconds is None:
            return
        platform_name = platform
        bridge_obj = bridge
        existing = self._output_flush_tasks.get(session_id)
        if existing and not existing.done():
            return

        async def _delayed_flush() -> None:
            try:
                await asyncio.sleep(delay_seconds)
            except asyncio.CancelledError:
                return
            self._output_flush_tasks.pop(session_id, None)
            await self._flush_output(session_id, platform_name, bridge_obj)

        self._output_flush_tasks[session_id] = asyncio.create_task(_delayed_flush())

    def _advance_replay_cursor(
        self, session_id: str, platform: str, seq: int, *, durable: bool = True
    ) -> None:
        """Track live consumption separately from crash-safe replay progress."""
        self._last_processed_seq[session_id] = max(
            self._last_processed_seq.get(session_id, 0), seq
        )
        pending_error = self._error_status_tasks.get(session_id)
        error_unpersisted = (
            pending_error is not None
            and not pending_error.done()
            and pending_error is not asyncio.current_task()
        )
        if (
            durable
            # Failed routes must be replayed on resubscription before checkpointing.
            and session_id not in self._failed_routing
            and not self._turns.buffered_size(session_id)
            and not error_unpersisted
        ):
            self._store.advance_bridge_replay_cursor(session_id, platform, seq)

    async def _replay_logged_events(
        self,
        session_id: str,
        platform: str,
        bridge: object,
        *,
        since_seq: int,
        before_seq: int | None = None,
    ) -> None:
        """Replay persisted events after a restart or live queue gap."""
        for event in self._store.read_event_log(
            session_id, since_seq=since_seq, before_seq=before_seq
        ):
            event_seq = self._event_seq(event)
            safe_to_advance = await self._route_event(
                session_id, platform, bridge, event
            )
            if event_seq is not None:
                self._advance_replay_cursor(
                    session_id, platform, event_seq, durable=safe_to_advance
                )

    async def _next_live_event(
        self, session_id: str, platform: str, queue: asyncio.Queue
    ) -> dict | None:
        """Wait for live input or wake up when an outbox retry becomes due."""
        delay = self._store.next_bridge_delivery_delay(session_id, platform)
        if delay is None:
            return await queue.get()
        try:
            return await asyncio.wait_for(queue.get(), timeout=max(0.0, delay))
        except asyncio.TimeoutError:
            return None

    async def _consume(
        self, session_id: str, platform: str, queue: asyncio.Queue
    ) -> None:
        """Read store subscriber events and route them to the platform bridge."""
        bridge = self._bridge_manager.get_bridge(platform)
        if not bridge:
            logger.warning(
                "No bridge for platform, subscriber exiting",
                session_id=session_id,
                platform=platform,
            )
            return

        try:
            cursor = self._store.bridge_replay_cursor(session_id, platform)
            self._last_processed_seq[session_id] = cursor.cursor_seq
            await self._replay_logged_events(
                session_id,
                platform,
                bridge,
                since_seq=cursor.cursor_seq,
            )
            await self._drain_pending_deliveries(session_id, platform, bridge)
            while True:
                event = await self._next_live_event(session_id, platform, queue)
                if event is None:
                    await self._drain_pending_deliveries(session_id, platform, bridge)
                    continue

                event_seq = self._event_seq(event)
                last_seq = self._last_processed_seq.get(session_id, 0)
                if event_seq is not None and event_seq > last_seq + 1:
                    await self._replay_logged_events(
                        session_id,
                        platform,
                        bridge,
                        since_seq=last_seq,
                        before_seq=event_seq,
                    )
                    last_seq = self._last_processed_seq.get(session_id, last_seq)
                if event_seq is not None and event_seq <= last_seq:
                    continue

                safe_to_advance = await self._route_event(
                    session_id, platform, bridge, event
                )
                if event_seq is not None:
                    self._advance_replay_cursor(
                        session_id, platform, event_seq, durable=safe_to_advance
                    )
        except asyncio.CancelledError:
            pass
        finally:
            self._remove_subscriber(session_id, queue)

    async def _route_event(
        self, session_id: str, platform: str, bridge: object, event: dict
    ) -> bool:
        """Route one store event and report whether replay may advance."""
        event_type = event.get("type")
        data = event.get("data", {})
        event_seq = self._event_seq(event)

        if data.get("is_history"):
            return True

        try:
            if event_type == "permission_resolved":
                request_id = str(data.get("request_id") or "")
                if request_id:
                    self._store.cancel_pending_permission_deliveries(
                        session_id, platform, request_id
                    )
                return True

            if event_type == "output":
                text = data.get("text", "")
                bridge_segments = (
                    data.get("bridge_segments")
                    if isinstance(data.get("bridge_segments"), list)
                    else None
                )
                if not text and not bridge_segments:
                    return self._turns.buffered_size(session_id) == 0
                is_final = bool(data.get("final") or data.get("is_final"))

                if self._is_confirmation_wait_output(bridge_segments):
                    return self._turns.buffered_size(session_id) == 0

                if is_final:
                    return self._turns.buffered_size(session_id) == 0

                segments = self._segments_from_output(text, bridge_segments)
                if any(segment.get("kind") == "tool_call" for segment in segments) and (
                    "assistant" in self._turns.buffered_segment_kinds(session_id)
                ):
                    # Tool execution separates progress prose from the final answer.
                    await self._flush_output(
                        session_id,
                        platform,
                        bridge,
                        delivery_key=self._delivery_key(
                            session_id, platform, "pre-tool-flush", event_seq
                        ),
                        event_seq=event_seq,
                        assistant_only=True,
                    )

                policy = self._policy(session_id)
                filtered_text, filtered_segments = self._filter_output_for_policy(
                    text, bridge_segments, policy
                )
                if not filtered_text and not filtered_segments:
                    return self._turns.buffered_size(session_id) == 0

                if filtered_segments and any(
                    segment.get("kind") == "warning" for segment in filtered_segments
                ):
                    await self._enqueue_and_deliver(
                        session_id=session_id,
                        platform=platform,
                        bridge=bridge,
                        delivery_key=self._delivery_key(
                            session_id, platform, "warning", event_seq
                        ),
                        method="on_output",
                        payload={
                            "text": filtered_text,
                            "metadata": {"bridge_segments": filtered_segments},
                        },
                        event_seq=event_seq,
                    )
                    return True

                self._buffer_output(
                    session_id,
                    filtered_text,
                    bridge_segments=filtered_segments,
                    event_seq=event_seq,
                )
                await self._schedule_flush(
                    session_id, platform, bridge, policy.buffer_max_seconds
                )

            elif event_type == "output_final":
                if self._has_only_buffered_tool_activity(session_id):
                    await self._flush_tool_activity(
                        session_id,
                        platform,
                        bridge,
                        delivery_key=self._delivery_key(
                            session_id, platform, "pre-final-tool", event_seq
                        ),
                        event_seq=event_seq,
                    )
                else:
                    await self._flush_output(
                        session_id,
                        platform,
                        bridge,
                        delivery_key=self._delivery_key(
                            session_id, platform, "pre-final-flush", event_seq
                        ),
                        event_seq=event_seq,
                        exclude_assistant=True,
                    )
                text = data.get("text", "")
                metadata = {
                    "final": True,
                    "kind": str(data.get("kind") or "final"),
                }
                final_segments = (
                    data.get("bridge_segments")
                    if isinstance(data.get("bridge_segments"), list)
                    else None
                )
                if final_segments:
                    metadata["bridge_segments"] = final_segments
                attachments = data.get("attachments")
                if attachments:
                    metadata["attachments"] = attachments
                warnings = data.get("attachment_warnings")
                if warnings:
                    metadata["attachment_warnings"] = warnings
                turn_id = data.get("turn_id")
                flush = self._turns.final_output(
                    session_id,
                    text,
                    metadata,
                    turn_id=str(turn_id) if turn_id else None,
                )
                if flush:
                    sent = await self._enqueue_and_deliver(
                        session_id=session_id,
                        platform=platform,
                        bridge=bridge,
                        delivery_key=self._delivery_key(
                            session_id, platform, "final", event_seq
                        ),
                        method="on_output",
                        payload={"text": flush.text, "metadata": flush.metadata},
                        event_seq=event_seq,
                    )
                    if sent:
                        self._turns.mark_final_sent(session_id, flush.final_key)

            elif event_type == "permission_request":
                request_id = str(data.get("request_id") or "")
                if request_id and self._store.permission_request_stale(
                    session_id, request_id
                ):
                    return True
                if self._has_only_buffered_tool_activity(session_id):
                    await self._flush_tool_activity(
                        session_id,
                        platform,
                        bridge,
                        delivery_key=self._delivery_key(
                            session_id, platform, "pre-permission-tool", event_seq
                        ),
                        event_seq=event_seq,
                    )
                else:
                    await self._flush_output(
                        session_id,
                        platform,
                        bridge,
                        delivery_key=self._delivery_key(
                            session_id, platform, "pre-permission-flush", event_seq
                        ),
                        event_seq=event_seq,
                    )
                request = self._build_approval_request(data)
                await self._enqueue_and_deliver(
                    session_id=session_id,
                    platform=platform,
                    bridge=bridge,
                    delivery_key=self._delivery_key(
                        session_id, platform, "permission", event_seq
                    ),
                    method="on_approval_request",
                    payload={"request": self._approval_request_payload(request)},
                    event_seq=event_seq,
                )

            elif event_type == "session_state":
                state = data.get("state", "")
                if state == "RUNNING":
                    await self._cancel_error_status(session_id)
                    self._turns.reset_turn(session_id)
                    await bridge.on_typing(session_id)
                elif state == "AWAITING_INPUT":
                    await self._cancel_error_status(session_id)
                    if self._has_only_buffered_tool_activity(session_id):
                        await self._flush_tool_activity(
                            session_id,
                            platform,
                            bridge,
                            delivery_key=self._delivery_key(
                                session_id, platform, "awaiting-tool", event_seq
                            ),
                            event_seq=event_seq,
                        )
                    else:
                        await self._flush_output(
                            session_id,
                            platform,
                            bridge,
                            delivery_key=self._delivery_key(
                                session_id, platform, "awaiting-flush", event_seq
                            ),
                            event_seq=event_seq,
                        )
                    await bridge.on_typing_stopped(session_id)
                elif state == "ERROR":
                    await self._flush_output(
                        session_id,
                        platform,
                        bridge,
                        delivery_key=self._delivery_key(
                            session_id, platform, "error-flush", event_seq
                        ),
                        event_seq=event_seq,
                    )
                    await bridge.on_typing_stopped(session_id)
                    self._schedule_error_status(session_id, platform, bridge, event_seq)
                    return False

            elif event_type == "error":
                await self._cancel_error_status(session_id)
                await self._flush_output(
                    session_id,
                    platform,
                    bridge,
                    delivery_key=self._delivery_key(
                        session_id, platform, "error-event-flush", event_seq
                    ),
                    event_seq=event_seq,
                )
                message = data.get("message", "Unknown error")
                await self._enqueue_and_deliver(
                    session_id=session_id,
                    platform=platform,
                    bridge=bridge,
                    delivery_key=self._delivery_key(
                        session_id, platform, "error-status", event_seq
                    ),
                    method="on_status_change",
                    payload={"status": "error", "metadata": {"message": message}},
                    event_seq=event_seq,
                )
                return True

        except Exception:
            self._failed_routing.add(session_id)
            logger.exception(
                "Failed to route event to bridge",
                session_id=session_id,
                event_type=event_type,
            )
            return False
        return self._turns.buffered_size(session_id) == 0

    def _build_approval_request(self, data: dict) -> ApprovalRequest:
        """Build a bridge approval request from permission event data."""
        tool_input = data.get("tool_input", {})
        tool_name = data.get("tool_name", "Permission request")

        if (
            isinstance(tool_input, dict)
            and str(tool_name).startswith("AskUserQuestion")
            and isinstance(tool_input.get("questions"), list)
            and tool_input["questions"]
            and isinstance(tool_input["questions"][0], dict)
        ):
            question = tool_input["questions"][0]
            header = str(question.get("header") or "Question")
            prompt = str(question.get("question") or "")
            options = question.get("options") or []
            labels: list[str] = []
            lines: list[str] = [prompt.strip()] if prompt else []
            for index, option in enumerate(options, start=1):
                if not isinstance(option, dict):
                    continue
                label = str(option.get("label") or "").strip()
                description = str(option.get("description") or "").strip()
                if not label:
                    continue
                labels.append(label)
                lines.append(
                    f"{index}. {label} - {description}"
                    if description
                    else f"{index}. {label}"
                )

            return ApprovalRequest(
                kind="choice",
                request_id=data.get("request_id", ""),
                title=header,
                description="\n".join(line for line in lines if line).strip(),
                options=labels,
            )

        description = (
            json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)
        )
        return ApprovalRequest(
            kind="permission",
            request_id=data.get("request_id", ""),
            title=tool_name,
            description=description,
            options=["Allow", "Deny"],
        )


def __getattr__(name: str) -> Any:
    """Lazy accessors for global bridge singletons."""

    if name in {"bridge_manager", "bridge_subscriber"}:
        from tether.bridges.glue import bridge_manager, bridge_subscriber

        return {
            "bridge_manager": bridge_manager,
            "bridge_subscriber": bridge_subscriber,
        }[name]
    raise AttributeError(name)
