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

    def subscribe(self, session_id: str, platform: str) -> None:
        """Start consuming store events for a session and routing to a bridge."""
        if session_id in self._tasks:
            return
        task = asyncio.create_task(self._consume(session_id, platform))
        self._tasks[session_id] = task
        logger.info(
            "Bridge subscriber started",
            session_id=session_id,
            platform=platform,
        )

    def unsubscribe(self, session_id: str) -> None:
        """Stop consuming events for a session."""
        task = self._tasks.pop(session_id, None)
        if task:
            task.cancel()
            logger.info("Bridge subscriber stopped", session_id=session_id)

    async def _consume(self, session_id: str, platform: str) -> None:
        """Background task that reads from a store subscriber and routes events."""
        from tether.store import store

        queue = store.new_subscriber(session_id)
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
                        description = json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)
                        request = ApprovalRequest(
                            request_id=data.get("request_id", ""),
                            title=data.get("tool_name", "Permission request"),
                            description=description,
                            options=["Allow", "Deny"],
                        )
                        await bridge.on_approval_request(session_id, request)

                    elif event_type == "session_state":
                        state = data.get("state", "")
                        if state == "RUNNING":
                            # Show typing indicator instead of a message
                            if hasattr(bridge, "on_typing"):
                                await bridge.on_typing(session_id)
                        elif state == "ERROR":
                            await bridge.on_status_change(session_id, "error")
                        # AWAITING_INPUT ("done") — no message needed

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
