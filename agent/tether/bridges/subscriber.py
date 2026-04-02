"""Tether-local bridge subscriber with final-output metadata passthrough."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from agent_tether.base import ApprovalRequest
from agent_tether.manager import BridgeManager
from agent_tether.subscriber import (
    _OUTPUT_FLUSH_DELAY_S,
    _OUTPUT_FLUSH_MAX_CHARS,
)

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
                bridge_manager as _bridge_manager,
                _new_subscriber,
                _remove_subscriber,
            )

            bridge_manager = bridge_manager or _bridge_manager
            new_subscriber = new_subscriber or _new_subscriber
            remove_subscriber = remove_subscriber or _remove_subscriber

        self._bridge_manager = bridge_manager
        self._new_subscriber = new_subscriber
        self._remove_subscriber = remove_subscriber
        self._tasks: dict[str, asyncio.Task] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._output_buffers: dict[str, list[str]] = {}
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
            "Bridge subscriber started",
            extra={"session_id": session_id, "platform": platform},
        )

    async def unsubscribe(
        self, session_id: str, *, platform: str | None = None
    ) -> None:
        """Stop consuming events for a session and clean up bridge state."""
        task = self._tasks.pop(session_id, None)
        self._queues.pop(session_id, None)
        if task:
            task.cancel()
            logger.info("Bridge subscriber stopped", extra={"session_id": session_id})

        if platform:
            bridge = self._bridge_manager.get_bridge(platform)
            if bridge:
                await self._flush_output(session_id, bridge)
                await bridge.on_session_removed(session_id)

    def _buffer_output(self, session_id: str, text: str) -> None:
        self._output_buffers.setdefault(session_id, []).append(text)

    def _buffer_size(self, session_id: str) -> int:
        return sum(len(text) for text in self._output_buffers.get(session_id, []))

    async def _flush_output(self, session_id: str, bridge: object) -> None:
        task = self._output_flush_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()

        buffered = self._output_buffers.pop(session_id, [])
        if not buffered:
            return

        text = "".join(buffered)
        if not text.strip():
            return

        try:
            await bridge.on_output(session_id, text)
        except Exception:
            logger.exception(
                "Failed to flush output to bridge",
                extra={"session_id": session_id},
            )

    async def _schedule_flush(self, session_id: str, bridge: object) -> None:
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
        """Background task that reads from a store subscriber and routes events."""
        bridge = self._bridge_manager.get_bridge(platform)
        if not bridge:
            logger.warning(
                "No bridge for platform, subscriber exiting",
                extra={"session_id": session_id, "platform": platform},
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
                        if not text:
                            continue
                        is_final = bool(data.get("final"))

                        if is_final:
                            await self._flush_output(session_id, bridge)
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
                            await bridge.on_output(
                                session_id,
                                text,
                                metadata=metadata,
                            )
                        else:
                            self._buffer_output(session_id, text)
                            await self._schedule_flush(session_id, bridge)

                    elif event_type == "output_final":
                        pass

                    elif event_type == "permission_request":
                        await self._flush_output(session_id, bridge)

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
                                description = str(
                                    option.get("description") or ""
                                ).strip()
                                if not label:
                                    continue
                                labels.append(label)
                                if description:
                                    lines.append(f"{index}. {label} - {description}")
                                else:
                                    lines.append(f"{index}. {label}")

                            request = ApprovalRequest(
                                kind="choice",
                                request_id=data.get("request_id", ""),
                                title=header,
                                description="\n".join(
                                    line for line in lines if line
                                ).strip(),
                                options=labels,
                            )
                        else:
                            description = (
                                json.dumps(tool_input)
                                if isinstance(tool_input, dict)
                                else str(tool_input)
                            )
                            request = ApprovalRequest(
                                kind="permission",
                                request_id=data.get("request_id", ""),
                                title=tool_name,
                                description=description,
                                options=["Allow", "Deny"],
                            )
                        await bridge.on_approval_request(session_id, request)

                    elif event_type == "session_state":
                        state = data.get("state", "")
                        if state == "RUNNING":
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
                            session_id,
                            "error",
                            {"message": message},
                        )

                except Exception:
                    logger.exception(
                        "Failed to route event to bridge",
                        extra={"session_id": session_id, "event_type": event_type},
                    )
        except asyncio.CancelledError:
            pass
        finally:
            self._remove_subscriber(session_id, queue)


def __getattr__(name: str) -> Any:
    """Lazy accessors for the global bridge singletons."""
    if name in {"bridge_subscriber", "bridge_manager"}:
        from tether.bridges.glue import bridge_manager, bridge_subscriber

        return {
            "bridge_manager": bridge_manager,
            "bridge_subscriber": bridge_subscriber,
        }[name]
    raise AttributeError(name)
