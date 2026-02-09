"""Bridge subscriber that routes store events to platform bridges."""

from __future__ import annotations

import asyncio
import json

import structlog

from tether.bridges.base import ApprovalRequest
from tether.bridges.manager import bridge_manager

logger = structlog.get_logger(__name__)


class BridgeSubscriber:
    """Subscribes to store events and routes them to platform bridges.

    For each session with a platform binding, a background task consumes
    events from a store subscriber queue and forwards them to the bridge.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._queues: dict[str, asyncio.Queue] = {}

    def subscribe(self, session_id: str, platform: str) -> None:
        """Start consuming store events for a session and routing to a bridge.

        The subscriber queue is registered synchronously so that events
        emitted between subscribe() and the first await in _consume()
        are not lost.
        """
        if session_id in self._tasks:
            return

        # Register the queue eagerly so no events are missed.
        from tether.store import store

        queue = store.new_subscriber(session_id)
        self._queues[session_id] = queue

        task = asyncio.create_task(self._consume(session_id, platform, queue))
        self._tasks[session_id] = task
        logger.info(
            "Bridge subscriber started",
            session_id=session_id,
            platform=platform,
        )

    async def unsubscribe(self, session_id: str, *, platform: str | None = None) -> None:
        """Stop consuming events for a session and clean up bridge state."""
        task = self._tasks.pop(session_id, None)
        self._queues.pop(session_id, None)
        if task:
            task.cancel()
            logger.info("Bridge subscriber stopped", session_id=session_id)

        # Notify bridge so it can clean up mappings
        if platform:
            bridge = bridge_manager.get_bridge(platform)
            if bridge:
                await bridge.on_session_removed(session_id)

    async def _consume(self, session_id: str, platform: str, queue: asyncio.Queue) -> None:
        """Background task that reads from a store subscriber and routes events."""
        bridge = bridge_manager.get_bridge(platform)
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

                # Skip history replay events
                if data.get("is_history"):
                    continue

                try:
                    if event_type == "output":
                        # Only forward the final assistant message of a turn.
                        # Intermediate steps (thinking, tool calls) have
                        # final=False / kind="step" and are skipped.
                        if data.get("final"):
                            text = data.get("text", "")
                            if text:
                                await bridge.on_output(session_id, text)

                    elif event_type == "output_final":
                        # Accumulated blob — skip, we use the per-step
                        # final output above instead.
                        pass

                    elif event_type == "permission_request":
                        tool_input = data.get("tool_input", {})
                        tool_name = data.get("tool_name", "Permission request")

                        # Special-case multi-choice questions coming through as a "tool".
                        # Codex emits these as AskUserQuestion with a structured schema.
                        if (
                            isinstance(tool_input, dict)
                            and str(tool_name).startswith("AskUserQuestion")
                            and isinstance(tool_input.get("questions"), list)
                            and tool_input["questions"]
                            and isinstance(tool_input["questions"][0], dict)
                        ):
                            q = tool_input["questions"][0]
                            header = str(q.get("header") or "Question")
                            question = str(q.get("question") or "")
                            options = q.get("options") or []
                            labels: list[str] = []
                            lines: list[str] = [question.strip()] if question else []
                            for i, opt in enumerate(options, start=1):
                                if not isinstance(opt, dict):
                                    continue
                                label = str(opt.get("label") or "").strip()
                                desc = str(opt.get("description") or "").strip()
                                if not label:
                                    continue
                                labels.append(label)
                                if desc:
                                    lines.append(f"{i}. {label} - {desc}")
                                else:
                                    lines.append(f"{i}. {label}")

                            request = ApprovalRequest(
                                kind="choice",
                                request_id=data.get("request_id", ""),
                                title=header,
                                description="\n".join([l for l in lines if l]).strip(),
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
                            await bridge.on_typing_stopped(session_id)
                        elif state == "ERROR":
                            await bridge.on_typing_stopped(session_id)
                            await bridge.on_status_change(session_id, "error")

                    elif event_type == "error":
                        msg = data.get("message", "Unknown error")
                        await bridge.on_status_change(
                            session_id, "error", {"message": msg}
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
            from tether.store import store as _store

            _store.remove_subscriber(session_id, queue)


bridge_subscriber = BridgeSubscriber()
