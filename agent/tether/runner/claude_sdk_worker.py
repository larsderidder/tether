"""Subprocess entry point for Claude Agent SDK isolation.

Run as: python -m tether.runner.claude_sdk_worker

Reads JSON-line commands from stdin, writes JSON-line events to stdout.
One process per query turn — exits cleanly after emitting a ``result`` event.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_event(event: dict) -> None:
    """Write a JSON-line event to stdout and flush immediately."""
    line = json.dumps(event, separators=(",", ":"))
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _read_line_sync() -> dict | None:
    """Read one JSON line from stdin (blocking)."""
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


# ---------------------------------------------------------------------------
# Async stdin reader (for permission responses while query runs)
# ---------------------------------------------------------------------------


class _StdinReader:
    """Non-blocking async wrapper around stdin for the child event loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: asyncio.StreamReader | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._reader = asyncio.StreamReader()
        transport, _ = await self._loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(self._reader),
            sys.stdin,
        )

    async def readline(self) -> dict | None:
        assert self._reader is not None
        raw = await self._reader.readline()
        if not raw:
            return None
        return json.loads(raw)


# ---------------------------------------------------------------------------
# Main worker logic
# ---------------------------------------------------------------------------


async def _run(start_cmd: dict) -> None:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        HookMatcher,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        query,
    )

    prompt = start_cmd["prompt"]
    cwd = start_cmd.get("cwd")
    permission_mode = start_cmd.get("permission_mode", "default")
    resume = start_cmd.get("resume")
    system_prompt = start_cmd.get("system_prompt")

    # Shared state
    stop_requested = False
    pending_permissions: dict[str, asyncio.Future] = {}

    # Set up async stdin reader for permission responses + stop commands
    stdin_reader = _StdinReader()
    await stdin_reader.start()

    # Background task: read stdin commands while query runs
    async def _stdin_loop() -> None:
        nonlocal stop_requested
        while True:
            try:
                msg = await stdin_reader.readline()
            except Exception:
                break
            if msg is None:
                break

            cmd = msg.get("cmd")
            if cmd == "permission_response":
                req_id = msg.get("request_id", "")
                fut = pending_permissions.pop(req_id, None)
                if fut and not fut.done():
                    fut.set_result(msg.get("behavior", "deny"))
            elif cmd == "stop":
                stop_requested = True
                # Deny all pending permissions to unblock hooks
                for fut in pending_permissions.values():
                    if not fut.done():
                        fut.set_result("deny")
                pending_permissions.clear()
                break

    stdin_task = asyncio.create_task(_stdin_loop())

    # Heartbeat
    start_time = time.monotonic()

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(5.0)
            _write_event({"event": "heartbeat", "elapsed_s": round(time.monotonic() - start_time, 1)})

    heartbeat_task = asyncio.create_task(_heartbeat())

    # PreToolUse hook for permission handling
    async def _pre_tool_use_hook(hook_input, tool_use_id, context):
        if stop_requested:
            return {
                "continue_": True,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "Session stopped",
                },
            }

        tool_name = hook_input.get("tool_name", "unknown")
        tool_input = hook_input.get("tool_input", {})
        request_id = f"perm_{id(hook_input):x}"

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending_permissions[request_id] = fut

        _write_event({
            "event": "permission_request",
            "request_id": request_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
        })

        try:
            behavior = await asyncio.wait_for(fut, timeout=300.0)
        except asyncio.TimeoutError:
            behavior = "deny"
        except asyncio.CancelledError:
            behavior = "deny"
        finally:
            pending_permissions.pop(request_id, None)

        if behavior == "allow":
            return {
                "continue_": True,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                },
            }
        else:
            return {
                "continue_": True,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "User denied permission",
                },
            }

    # Build options
    hooks = None
    if permission_mode != "bypassPermissions":
        hooks = {"PreToolUse": [HookMatcher(hooks=[_pre_tool_use_hook])]}

    def stderr_handler(line: str) -> None:
        _write_event({"event": "stderr", "line": line})

    options = ClaudeAgentOptions(
        cwd=Path(cwd) if cwd else None,
        permission_mode=permission_mode,
        resume=resume,
        # Do NOT set continue_conversation=True — it adds --continue which
        # resolves to the most recent session in the cwd. With multiple Tether
        # sessions in the same directory, --continue could pick the WRONG
        # session causing cross-talk.  --resume alone is sufficient.
        continue_conversation=False,
        setting_sources=[],
        system_prompt=system_prompt or "",
        stderr=stderr_handler,
        hooks=hooks,
    )

    # Keep stdin open for hook control protocol
    query_done = asyncio.Event()

    async def prompt_stream():
        yield {"type": "user", "message": {"role": "user", "content": prompt}}
        if hooks:
            await query_done.wait()

    try:
        query_stream = query(prompt=prompt_stream(), options=options)
        try:
            async for message in query_stream:
                if stop_requested:
                    break
                _handle_message(message)
                if isinstance(message, ResultMessage):
                    query_done.set()
                    break
        finally:
            query_done.set()
            try:
                await query_stream.aclose()
            except (Exception, asyncio.CancelledError, GeneratorExit):
                # The SDK's anyio cancel scopes raise RuntimeError
                # ("exit cancel scope in different task") during
                # generator cleanup.  CancelledError can surface from
                # the SDK's internal task group.  All harmless here —
                # the query is already finished.
                pass
    except Exception as exc:
        _write_event({"event": "error", "code": "SDK_ERROR", "message": str(exc)})
    finally:
        heartbeat_task.cancel()
        stdin_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        try:
            await stdin_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Message serialization (child side)
# ---------------------------------------------------------------------------


def _handle_message(message) -> None:
    """Serialize an SDK message to JSON-line events on stdout."""
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
    )

    if isinstance(message, SystemMessage):
        if message.subtype == "init":
            data = message.data
            _write_event({
                "event": "init",
                "session_id": data.get("session_id"),
                "model": data.get("model"),
                "version": data.get("claude_code_version"),
            })
    elif isinstance(message, AssistantMessage):
        if message.error:
            _write_event({"event": "error", "code": "ASSISTANT_ERROR", "message": message.error})
            return
        blocks = _serialize_blocks(message.content)
        if blocks:
            _write_event({"event": "output", "blocks": blocks})
    elif isinstance(message, ResultMessage):
        usage = message.usage or {}
        _write_event({
            "event": "result",
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cost_usd": message.total_cost_usd,
            "is_error": message.is_error,
            "error_text": message.result if message.is_error else None,
        })


def _serialize_blocks(content: list) -> list[dict]:
    """Convert SDK content blocks to plain dicts."""
    from claude_agent_sdk import (
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
    )

    blocks: list[dict] = []
    for block in content:
        if isinstance(block, TextBlock):
            blocks.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            blocks.append({
                "type": "tool_use",
                "name": block.name,
                "id": block.id,
                "input": block.input if isinstance(block.input, dict) else {},
            })
        elif isinstance(block, ToolResultBlock):
            c = block.content
            blocks.append({
                "type": "tool_result",
                "content": c if isinstance(c, str) else str(c),
                "is_error": block.is_error,
            })
        elif isinstance(block, ThinkingBlock):
            if block.thinking:
                blocks.append({"type": "thinking", "thinking": block.thinking})
    return blocks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Read a start command from stdin and run the query."""
    start_cmd = _read_line_sync()
    if not start_cmd or start_cmd.get("cmd") != "start":
        _write_event({"event": "error", "code": "BAD_START", "message": "Expected start command"})
        sys.exit(1)

    # Manual event loop management instead of asyncio.run() to suppress
    # noisy but harmless cleanup errors from the SDK's anyio cancel
    # scopes and subprocess transport __del__ during shutdown.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run(start_cmd))
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        except Exception:
            pass
        finally:
            asyncio.set_event_loop(None)
            loop.close()


if __name__ == "__main__":
    main()
