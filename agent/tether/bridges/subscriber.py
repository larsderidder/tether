"""Bridge subscriber with Tether-local output metadata support."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any, Callable

import structlog
from agent_tether.base import ApprovalRequest
from agent_tether.manager import BridgeManager

from tether.bridges.rich_output_segments import parse_output_segments
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
        self._output_flush_tasks: dict[str, asyncio.Task] = {}
        self._error_status_tasks: dict[str, asyncio.Task] = {}

    def subscribe(self, session_id: str, platform: str) -> None:
        """Start consuming store events for a session and routing to a bridge."""
        if session_id in self._tasks:
            return

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

        if platform:
            bridge = self._bridge_manager.get_bridge(platform)
            if bridge:
                await self._flush_output(session_id, bridge)
                await bridge.on_session_removed(session_id)
        self._turns.remove(session_id)

    def _buffer_output(
        self,
        session_id: str,
        text: str,
        bridge_segments: list[dict[str, str]] | None = None,
    ) -> None:
        """Add text and optional structured bridge segments to the output buffer."""
        self._turns.buffer_stream(session_id, text, bridge_segments)

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
        allowed = {"thinking", "warning"}
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

    async def _flush_output(self, session_id: str, bridge: object) -> None:
        """Send all buffered output for a session to the bridge."""
        task = self._output_flush_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()

        flush = self._turns.flush_stream(session_id)
        if not flush:
            return

        try:
            await bridge.on_output(session_id, flush.text, metadata=flush.metadata)
        except Exception:
            logger.exception("Failed to flush output to bridge", session_id=session_id)

    async def _flush_tool_activity(self, session_id: str, bridge: object) -> None:
        """Send buffered tool telemetry as one bridge message."""
        flush = self._turns.flush_tool_activity(session_id)
        if not flush:
            return

        try:
            await bridge.on_output(session_id, flush.text, metadata=flush.metadata)
        except Exception:
            logger.exception(
                "Failed to flush tool activity to bridge", session_id=session_id
            )

    async def _cancel_error_status(self, session_id: str) -> None:
        """Cancel a generic error status that is waiting for richer metadata."""
        task = self._error_status_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _schedule_error_status(self, session_id: str, bridge: object) -> None:
        """Delay a generic error so the following detailed event can replace it."""
        existing = self._error_status_tasks.get(session_id)
        if existing and not existing.done():
            return

        async def _delayed_status() -> None:
            try:
                await asyncio.sleep(_ERROR_STATUS_COALESCE_S)
                await bridge.on_status_change(session_id, "error")
            except asyncio.CancelledError:
                return
            finally:
                current = self._error_status_tasks.get(session_id)
                if current is asyncio.current_task():
                    self._error_status_tasks.pop(session_id, None)

        self._error_status_tasks[session_id] = asyncio.create_task(_delayed_status())

    async def _schedule_flush(
        self, session_id: str, bridge: object, delay_seconds: float | None
    ) -> None:
        """Schedule a delayed flush of buffered output when policy allows it."""
        if delay_seconds is None:
            return
        existing = self._output_flush_tasks.get(session_id)
        if existing and not existing.done():
            return

        async def _delayed_flush() -> None:
            try:
                await asyncio.sleep(delay_seconds)
            except asyncio.CancelledError:
                return
            self._output_flush_tasks.pop(session_id, None)
            await self._flush_output(session_id, bridge)

        self._output_flush_tasks[session_id] = asyncio.create_task(_delayed_flush())

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
            while True:
                event = await queue.get()
                event_type = event.get("type")
                data = event.get("data", {})

                if data.get("is_history"):
                    continue

                try:
                    if event_type == "output":
                        text = data.get("text", "")
                        bridge_segments = (
                            data.get("bridge_segments")
                            if isinstance(data.get("bridge_segments"), list)
                            else None
                        )
                        if not text and not bridge_segments:
                            continue
                        is_final = bool(data.get("final") or data.get("is_final"))

                        if self._is_confirmation_wait_output(bridge_segments):
                            continue

                        if is_final:
                            # finalize_output emits an output_final aggregate
                            # immediately after final output events. Route only
                            # that aggregate to chat bridges, otherwise users
                            # see the same answer multiple times.
                            continue

                        policy = self._policy(session_id)
                        filtered_text, filtered_segments = (
                            self._filter_output_for_policy(
                                text, bridge_segments, policy
                            )
                        )
                        if not filtered_text and not filtered_segments:
                            continue

                        if filtered_segments and any(
                            segment.get("kind") == "warning"
                            for segment in filtered_segments
                        ):
                            await bridge.on_output(
                                session_id,
                                filtered_text,
                                metadata={"bridge_segments": filtered_segments},
                            )
                            continue

                        self._buffer_output(
                            session_id,
                            filtered_text,
                            bridge_segments=filtered_segments,
                        )
                        await self._schedule_flush(
                            session_id, bridge, policy.buffer_max_seconds
                        )

                    elif event_type == "output_final":
                        if self._has_only_buffered_tool_activity(session_id):
                            await self._flush_tool_activity(session_id, bridge)
                        else:
                            await self._flush_output(session_id, bridge)
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
                            await bridge.on_output(
                                session_id, flush.text, metadata=flush.metadata
                            )
                            self._turns.mark_final_sent(session_id, flush.final_key)

                    elif event_type == "permission_request":
                        if self._has_only_buffered_tool_activity(session_id):
                            await self._flush_tool_activity(session_id, bridge)
                        else:
                            await self._flush_output(session_id, bridge)
                        request = self._build_approval_request(data)
                        await bridge.on_approval_request(session_id, request)

                    elif event_type == "session_state":
                        state = data.get("state", "")
                        if state == "RUNNING":
                            await self._cancel_error_status(session_id)
                            self._turns.reset_turn(session_id)
                            await bridge.on_typing(session_id)
                        elif state == "AWAITING_INPUT":
                            await self._cancel_error_status(session_id)
                            if self._has_only_buffered_tool_activity(session_id):
                                await self._flush_tool_activity(session_id, bridge)
                            else:
                                await self._flush_output(session_id, bridge)
                            await bridge.on_typing_stopped(session_id)
                        elif state == "ERROR":
                            await self._flush_output(session_id, bridge)
                            await bridge.on_typing_stopped(session_id)
                            self._schedule_error_status(session_id, bridge)

                    elif event_type == "error":
                        await self._cancel_error_status(session_id)
                        await self._flush_output(session_id, bridge)
                        message = data.get("message", "Unknown error")
                        await bridge.on_status_change(
                            session_id, "error", {"message": message}
                        )

                except Exception:
                    logger.exception(
                        "Failed to route event to bridge",
                        session_id=session_id,
                        event_type=event_type,
                    )
        except asyncio.CancelledError:
            pass
        finally:
            self._remove_subscriber(session_id, queue)

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
