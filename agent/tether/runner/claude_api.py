"""Runner adapter that uses the Anthropic Python SDK directly."""

from __future__ import annotations

import asyncio
import json
import time

import structlog
from anthropic import Anthropic

from tether.models import SessionState
from tether.prompts import SYSTEM_PROMPT
from tether.runner.base import RunnerEvents
from tether.settings import settings
from tether.store import store
from tether.tools import TOOLS, execute_tool

logger = structlog.get_logger(__name__)


class ClaudeRunner:
    """Runner that uses the Anthropic Python SDK directly."""

    runner_type: str = "claude_api"

    def __init__(self, events: RunnerEvents) -> None:
        self._events = events
        self._client = Anthropic()
        self._model = settings.claude_model()
        self._max_tokens = settings.claude_max_tokens()
        self._tasks: dict[str, asyncio.Task] = {}

    async def start(self, session_id: str, prompt: str, approval_choice: int) -> None:
        """Start a Claude conversation session.

        Args:
            session_id: Internal session identifier.
            prompt: Initial prompt to send.
            approval_choice: Approval policy (ignored - auto-approve all).
        """
        # Clear any previous state
        store.clear_stop_requested(session_id)
        self._tasks.pop(session_id, None)

        # Emit header
        await self._events.on_header(
            session_id,
            title="Claude API",
            model=self._model,
            provider="Anthropic",
        )

        # Add user message to history
        store.add_message(session_id, "user", [{"type": "text", "text": prompt}])

        # Start conversation loop
        task = asyncio.create_task(self._conversation_loop(session_id))
        self._tasks[session_id] = task

    async def send_input(self, session_id: str, text: str) -> None:
        """Send follow-up input to the conversation.

        Args:
            session_id: Internal session identifier.
            text: Follow-up input text.
        """
        if not text.strip():
            return

        # Add user message to history
        store.add_message(session_id, "user", [{"type": "text", "text": text}])

        # If no active task, start a new conversation loop
        task = self._tasks.get(session_id)
        if task is None or task.done():
            store.clear_stop_requested(session_id)
            task = asyncio.create_task(self._conversation_loop(session_id))
            self._tasks[session_id] = task

    async def stop(self, session_id: str) -> int | None:
        """Stop the Claude session.

        Args:
            session_id: Internal session identifier.

        Returns:
            Exit code (always 0 for clean stop).
        """
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

    async def _conversation_loop(self, session_id: str) -> None:
        """Main conversation loop that handles the agentic flow.

        Args:
            session_id: Internal session identifier.
        """
        start_time = time.monotonic()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(session_id, start_time)
        )

        try:
            while not store.is_stop_requested(session_id):
                # Get session to check state
                session = store.get_session(session_id)
                if not session or session.state != SessionState.RUNNING:
                    break

                # Get conversation history
                messages = store.get_messages(session_id)
                if not messages:
                    break

                # Make API call with streaming
                response = await self._call_api(session_id, messages)
                if response is None:
                    break

                stop_reason = response.get("stop_reason")
                content_blocks = response.get("content", [])
                usage = response.get("usage", {})

                # Report token usage
                if usage:
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    await self._events.on_metadata(
                        session_id,
                        "tokens",
                        {"input": input_tokens, "output": output_tokens},
                        f"input: {input_tokens}, output: {output_tokens}",
                    )

                # Save assistant response
                store.add_message(session_id, "assistant", content_blocks)

                # Check for tool use
                tool_uses = [b for b in content_blocks if b.get("type") == "tool_use"]

                if tool_uses:
                    # Execute tools and collect results
                    tool_results = []
                    for tool_use in tool_uses:
                        tool_name = tool_use.get("name")
                        tool_input = tool_use.get("input", {})
                        tool_id = tool_use.get("id")

                        # Emit tool invocation
                        await self._events.on_output(
                            session_id,
                            "combined",
                            f"[tool: {tool_name}] {json.dumps(tool_input)}\n",
                            kind="step",
                            is_final=False,
                        )

                        # Execute tool
                        result = await execute_tool(session_id, tool_name, tool_input)

                        # Format result
                        if result.get("success"):
                            content = result.get("result", "")
                        else:
                            content = f"Error: {result.get('error', 'Unknown error')}"

                        # Emit tool result
                        truncated = content[:500] + "..." if len(content) > 500 else content
                        await self._events.on_output(
                            session_id,
                            "combined",
                            f"[result] {truncated}\n",
                            kind="step",
                            is_final=False,
                        )

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": content,
                        })

                    # Add tool results as user message and continue loop
                    store.add_message(session_id, "user", tool_results)
                    continue

                # No tool use - conversation turn complete
                if stop_reason == "end_turn":
                    break

                # Max tokens reached
                if stop_reason == "max_tokens":
                    await self._events.on_output(
                        session_id,
                        "combined",
                        "\n[max tokens reached]\n",
                        kind="step",
                        is_final=False,
                    )
                    break

                # Unknown stop reason
                break

        except asyncio.CancelledError:
            logger.info("Claude conversation cancelled", session_id=session_id)
        except Exception as e:
            logger.exception("Claude conversation failed", session_id=session_id)
            await self._events.on_error(session_id, "CLAUDE_ERROR", str(e))
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

            # Emit final heartbeat
            elapsed = time.monotonic() - start_time
            await self._events.on_heartbeat(session_id, elapsed, done=True)

            # If stop was requested, emit exit; otherwise signal awaiting input
            if store.is_stop_requested(session_id):
                await self._events.on_exit(session_id, 0)
            else:
                await self._events.on_awaiting_input(session_id)

    async def _call_api(self, session_id: str, messages: list[dict]) -> dict | None:
        """Call the Anthropic API with streaming.

        Args:
            session_id: Internal session identifier.
            messages: Conversation history.

        Returns:
            Response dict with content, stop_reason, and usage.
        """
        try:
            # Use streaming API
            content_blocks: list[dict] = []
            current_text = ""
            usage = {}
            stop_reason = None

            with self._client.messages.stream(
                model=self._model,
                max_tokens=self._max_tokens,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=TOOLS,
            ) as stream:
                for event in stream:
                    # Check for stop request
                    if store.is_stop_requested(session_id):
                        return None

                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "text":
                            current_text = ""
                        elif block.type == "tool_use":
                            content_blocks.append({
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": {},
                            })

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            current_text += delta.text
                            # Stream text output
                            await self._events.on_output(
                                session_id,
                                "combined",
                                delta.text,
                                kind="final",
                                is_final=True,
                            )

                    elif event.type == "content_block_stop":
                        if current_text:
                            content_blocks.append({
                                "type": "text",
                                "text": current_text,
                            })
                            current_text = ""

                    elif event.type == "message_delta":
                        if hasattr(event, "delta"):
                            stop_reason = getattr(event.delta, "stop_reason", None)
                        if hasattr(event, "usage"):
                            usage["output_tokens"] = getattr(event.usage, "output_tokens", 0)

                    elif event.type == "message_start":
                        if hasattr(event, "message") and hasattr(event.message, "usage"):
                            usage["input_tokens"] = getattr(event.message.usage, "input_tokens", 0)

                # Get final message for complete tool inputs
                final_message = stream.get_final_message()
                if final_message:
                    # Replace content blocks with final parsed versions
                    content_blocks = []
                    for block in final_message.content:
                        if block.type == "text":
                            content_blocks.append({
                                "type": "text",
                                "text": block.text,
                            })
                        elif block.type == "tool_use":
                            content_blocks.append({
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            })
                    stop_reason = final_message.stop_reason

            return {
                "content": content_blocks,
                "stop_reason": stop_reason,
                "usage": usage,
            }

        except Exception as e:
            logger.exception("API call failed", session_id=session_id)
            raise

    async def _heartbeat_loop(self, session_id: str, start_time: float) -> None:
        """Emit periodic heartbeats while the conversation is active.

        Args:
            session_id: Internal session identifier.
            start_time: Monotonic timestamp when conversation started.
        """
        interval_s = 5.0  # Heartbeat interval in seconds
        try:
            while True:
                await asyncio.sleep(interval_s)
                elapsed = time.monotonic() - start_time
                await self._events.on_heartbeat(session_id, elapsed, done=False)
        except asyncio.CancelledError:
            pass
