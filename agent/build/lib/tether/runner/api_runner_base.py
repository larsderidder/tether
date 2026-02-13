"""Shared base class for API-based runner adapters (Claude, LiteLLM, etc.)."""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from typing import Any

import structlog

from tether.models import SessionState
from tether.runner.base import RunnerEvents
from tether.store import store
from tether.tools import execute_tool

logger = structlog.get_logger(__name__)


class ApiRunnerBase(ABC):
    """Base class for runners that call an LLM API directly with tool use.

    Subclasses implement the API-specific parts: message formatting, API calls,
    and response parsing.  The base class owns the conversation loop, heartbeats,
    tool execution, stop handling, and event emission.
    """

    runner_type: str  # Set by subclass

    def __init__(self, events: RunnerEvents) -> None:
        self._events = events
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Runner protocol
    # ------------------------------------------------------------------

    async def start(self, session_id: str, prompt: str, approval_choice: int) -> None:
        store.clear_stop_requested(session_id)
        self._tasks.pop(session_id, None)

        await self._emit_header(session_id)
        self._add_user_message(session_id, prompt)

        task = asyncio.create_task(self._conversation_loop(session_id))
        self._tasks[session_id] = task

    async def send_input(self, session_id: str, text: str) -> None:
        if not text.strip():
            return
        self._add_user_message(session_id, text)
        task = self._tasks.get(session_id)
        if task is None or task.done():
            store.clear_stop_requested(session_id)
            task = asyncio.create_task(self._conversation_loop(session_id))
            self._tasks[session_id] = task

    async def stop(self, session_id: str) -> int | None:
        store.request_stop(session_id)
        task = self._tasks.get(session_id)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._tasks.pop(session_id, None)
        store.clear_stop_requested(session_id)
        return 0

    # ------------------------------------------------------------------
    # Conversation loop (shared)
    # ------------------------------------------------------------------

    async def _conversation_loop(self, session_id: str) -> None:
        start_time = time.monotonic()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(session_id, start_time)
        )

        try:
            while not store.is_stop_requested(session_id):
                session = store.get_session(session_id)
                if not session or session.state != SessionState.RUNNING:
                    break

                messages = store.get_messages(session_id)
                if not messages:
                    break

                response = await self._call_api(session_id, messages)
                if response is None:
                    break

                stop_reason = response.get("stop_reason")
                content_blocks = response.get("content", [])
                usage = response.get("usage", {})

                if usage:
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    await self._events.on_metadata(
                        session_id,
                        "tokens",
                        {"input": input_tokens, "output": output_tokens},
                        f"input: {input_tokens}, output: {output_tokens}",
                    )

                self._save_assistant_response(session_id, content_blocks)

                tool_uses = self._extract_tool_uses(content_blocks)

                if tool_uses:
                    tool_results = await self._execute_and_emit_tools(
                        session_id, tool_uses
                    )
                    self._add_tool_results(session_id, tool_uses, tool_results)
                    continue

                if stop_reason == "end_turn":
                    break

                if stop_reason == "max_tokens":
                    await self._events.on_output(
                        session_id,
                        "combined",
                        "\n[max tokens reached]\n",
                        kind="step",
                        is_final=False,
                    )
                    break

                # Unknown stop reason — don't loop forever
                break

        except asyncio.CancelledError:
            logger.info("Conversation cancelled", session_id=session_id)
        except Exception as e:
            logger.exception("Conversation failed", session_id=session_id)
            await self._events.on_error(session_id, "RUNNER_ERROR", str(e))
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

            elapsed = time.monotonic() - start_time
            await self._events.on_heartbeat(session_id, elapsed, done=True)

            if store.is_stop_requested(session_id):
                await self._events.on_exit(session_id, 0)
            else:
                await self._events.on_awaiting_input(session_id)

    # ------------------------------------------------------------------
    # Tool execution (shared)
    # ------------------------------------------------------------------

    async def _execute_and_emit_tools(
        self, session_id: str, tool_uses: list[dict]
    ) -> list[dict[str, Any]]:
        """Execute tools and emit output events. Returns list of result dicts."""
        results: list[dict[str, Any]] = []
        for tool_use in tool_uses:
            tool_name = tool_use.get("name")
            tool_input = tool_use.get("input", {})

            await self._events.on_output(
                session_id,
                "combined",
                f"[tool: {tool_name}] {json.dumps(tool_input)}\n",
                kind="step",
                is_final=False,
            )

            result = await execute_tool(session_id, tool_name, tool_input)

            if result.get("success"):
                content = result.get("result", "")
            else:
                content = f"Error: {result.get('error', 'Unknown error')}"

            truncated = content[:500] + "..." if len(content) > 500 else content
            await self._events.on_output(
                session_id,
                "combined",
                f"[result] {truncated}\n",
                kind="step",
                is_final=False,
            )

            results.append({"tool_use": tool_use, "content": content})

        return results

    # ------------------------------------------------------------------
    # Heartbeat (shared)
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, session_id: str, start_time: float) -> None:
        interval_s = 5.0
        try:
            while True:
                await asyncio.sleep(interval_s)
                elapsed = time.monotonic() - start_time
                await self._events.on_heartbeat(session_id, elapsed, done=False)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Abstract methods — subclass implements
    # ------------------------------------------------------------------

    @abstractmethod
    async def _emit_header(self, session_id: str) -> None:
        """Emit on_header with runner-specific title/model/provider."""
        ...

    @abstractmethod
    async def _call_api(
        self, session_id: str, messages: list[dict]
    ) -> dict | None:
        """Call the LLM API. Return dict with content, stop_reason, usage or None."""
        ...

    @abstractmethod
    def _add_user_message(self, session_id: str, text: str) -> None:
        """Add a user message to the store in the format expected by the API."""
        ...

    @abstractmethod
    def _save_assistant_response(
        self, session_id: str, content_blocks: list[dict]
    ) -> None:
        """Persist the assistant response in the store."""
        ...

    @abstractmethod
    def _extract_tool_uses(self, content_blocks: list[dict]) -> list[dict]:
        """Extract tool_use blocks from the response content."""
        ...

    @abstractmethod
    def _add_tool_results(
        self,
        session_id: str,
        tool_uses: list[dict],
        results: list[dict[str, Any]],
    ) -> None:
        """Add tool results to the message store for the next API call."""
        ...
