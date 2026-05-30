"""Bridge subscriber with Tether-local output metadata support."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from agent_tether.base import ApprovalRequest
from agent_tether.manager import BridgeManager
from agent_tether.subscriber import _OUTPUT_FLUSH_DELAY_S, _OUTPUT_FLUSH_MAX_CHARS

from tether.bridges.turn_accumulator import BridgeTurnAccumulator

logger = structlog.get_logger(__name__)


class BridgeSubscriber:
    """Subscribe to store events and route them to platform bridges."""

    def __init__(
        self,
        bridge_manager: BridgeManager | None = None,
        new_subscriber=None,
        remove_subscriber=None,
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
        self._tasks: dict[str, asyncio.Task] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._turns = BridgeTurnAccumulator()
        self._output_flush_tasks: dict[str, asyncio.Task] = {}

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

    def _buffer_size(self, session_id: str) -> int:
        """Return the total character count in the output buffer."""
        return self._turns.buffered_size(session_id)

    @staticmethod
    def _is_streaming_prose(
        bridge_segments: list[dict[str, str]] | None,
    ) -> bool:
        """Return true for assistant prose tokens that should wait for final text."""

        if not bridge_segments:
            return False
        prose_kinds = {"assistant", "thinking"}
        return all(str(segment.get("kind") or "") in prose_kinds for segment in bridge_segments)

    def _discard_buffered_output(self, session_id: str) -> None:
        """Drop buffered streaming output for a session."""

        task = self._output_flush_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
        self._turns.discard(session_id)

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

    async def _schedule_flush(self, session_id: str, bridge: object) -> None:
        """Schedule a delayed flush of buffered output."""
        existing = self._output_flush_tasks.pop(session_id, None)
        if existing and not existing.done():
            existing.cancel()

        if self._buffer_size(session_id) >= _OUTPUT_FLUSH_MAX_CHARS:
            await self._flush_output(session_id, bridge)
            return

        async def _delayed_flush() -> None:
            try:
                await asyncio.sleep(_OUTPUT_FLUSH_DELAY_S)
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

                        if is_final:
                            # finalize_output emits an output_final aggregate
                            # immediately after final output events. Route only
                            # that aggregate to chat bridges, otherwise users
                            # see the same answer multiple times.
                            continue
                        else:
                            self._buffer_output(
                                session_id, text, bridge_segments=bridge_segments
                            )
                            if not self._is_streaming_prose(bridge_segments):
                                await self._schedule_flush(session_id, bridge)

                    elif event_type == "output_final":
                        self._discard_buffered_output(session_id)
                        text = data.get("text", "")
                        metadata = {
                            "final": True,
                            "kind": str(data.get("kind") or "final"),
                        }
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
                        await self._flush_output(session_id, bridge)
                        request = self._build_approval_request(data)
                        await bridge.on_approval_request(session_id, request)

                    elif event_type == "session_state":
                        state = data.get("state", "")
                        if state == "RUNNING":
                            self._turns.reset_turn(session_id)
                            await bridge.on_typing(session_id)
                        elif state == "AWAITING_INPUT":
                            await self._flush_output(session_id, bridge)
                            await bridge.on_typing_stopped(session_id)
                        elif state == "ERROR":
                            await self._flush_output(session_id, bridge)
                            await bridge.on_typing_stopped(session_id)
                            await bridge.on_status_change(session_id, "error")

                    elif event_type == "error":
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
