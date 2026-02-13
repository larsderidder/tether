"""Runner adapter that isolates the Claude Agent SDK in a subprocess per query.

Each ``start()`` or ``send_input()`` call spawns a fresh child process running
``claude_sdk_worker.py``.  Communication happens over JSON lines on stdin/stdout,
keeping all SDK state (async generators, OAuth, etc.) outside Tether's event loop.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid

import structlog

from tether.discovery.running import is_claude_session_running
from tether.prompts import SYSTEM_PROMPT
from tether.runner.base import RunnerEvents
from tether.store import store

logger = structlog.get_logger(__name__)

HEARTBEAT_INTERVAL = 5.0
PERMISSION_TIMEOUT = 300.0

# SDK/CLI cleanup noise that we don't want to spam into tether.log.
_IGNORED_WORKER_STDERR_SUBSTRINGS: tuple[str, ...] = (
    "Attempted to exit cancel scope in a different task than it was entered in",
    "unhandled exception during asyncio.run() shutdown",
    "Exception ignored in: <function BaseSubprocessTransport.__del__",
    "RuntimeError: Event loop is closed",
    "child process pid",
)


class ClaudeSubprocessRunner:
    """Runner that spawns one subprocess per query turn via claude_sdk_worker."""

    runner_type: str = "claude-subprocess"

    def __init__(self, events: RunnerEvents) -> None:
        self._events = events
        self._sdk_sessions: dict[str, str] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._readers: dict[str, asyncio.Task] = {}
        self._permission_modes: dict[str, str] = {}
        self._pending_inputs: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Runner protocol
    # ------------------------------------------------------------------

    async def start(self, session_id: str, prompt: str, approval_choice: int) -> None:
        permission_mode = self._map_permission_mode(approval_choice)
        self._permission_modes[session_id] = permission_mode

        logger.info(
            "Starting claude_subprocess session",
            session_id=session_id,
            approval_choice=approval_choice,
            permission_mode=permission_mode,
        )
        store.clear_stop_requested(session_id)

        session = store.get_session(session_id)
        cwd = session.directory if session and session.directory else None

        # Prefer in-memory cache (updated on session expiry) over store
        resume = self._sdk_sessions.get(session_id)
        if not resume:
            resume = store.get_runner_session_id(session_id)
            if resume:
                self._sdk_sessions[session_id] = resume

        resume = self._maybe_drop_busy_resume(session_id, resume)
        await self._spawn(session_id, prompt, cwd, permission_mode, resume)

    async def send_input(self, session_id: str, text: str) -> None:
        if not text.strip():
            return

        sdk_session_id = self._sdk_sessions.get(session_id)
        if not sdk_session_id:
            sdk_session_id = store.get_runner_session_id(session_id)
            if sdk_session_id:
                self._sdk_sessions[session_id] = sdk_session_id

        session = store.get_session(session_id)
        cwd = session.directory if session and session.directory else None

        # If a process is still running, queue the input
        proc = self._processes.get(session_id)
        if proc and proc.returncode is None:
            logger.warning(
                "send_input called while subprocess still running; queueing",
                session_id=session_id,
            )
            self._pending_inputs.setdefault(session_id, []).append(text)
            return

        store.clear_stop_requested(session_id)

        permission_mode = self._permission_modes.get(session_id)
        if not permission_mode:
            approval_mode = session.approval_mode if session else None
            permission_mode = self._map_permission_mode(approval_mode or 0)

        sdk_session_id = self._maybe_drop_busy_resume(session_id, sdk_session_id)
        await self._spawn(
            session_id, text, cwd, permission_mode, resume=sdk_session_id
        )

    async def stop(self, session_id: str) -> int | None:
        store.request_stop(session_id)
        store.clear_pending_permissions(session_id)

        proc = self._processes.get(session_id)
        if proc and proc.returncode is None:
            # Send stop command first
            self._write_cmd(proc, {"cmd": "stop"})
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Subprocess did not exit in time, killing", session_id=session_id)
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

        # Cancel the reader task
        reader = self._readers.pop(session_id, None)
        if reader and not reader.done():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass

        self._processes.pop(session_id, None)
        self._pending_inputs.pop(session_id, None)
        store.clear_stop_requested(session_id)
        return 0

    def update_permission_mode(self, session_id: str, approval_choice: int) -> None:
        permission_mode = self._map_permission_mode(approval_choice)
        self._permission_modes[session_id] = permission_mode
        logger.info(
            "Updated permission mode",
            session_id=session_id,
            approval_choice=approval_choice,
            permission_mode=permission_mode,
        )

    # ------------------------------------------------------------------
    # Internal: subprocess lifecycle
    # ------------------------------------------------------------------

    async def _spawn(
        self,
        session_id: str,
        prompt: str,
        cwd: str | None,
        permission_mode: str,
        resume: str | None,
    ) -> None:
        """Spawn a worker subprocess and start the reader task."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tether.runner.claude_sdk_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._processes[session_id] = proc
        store.set_process(session_id, proc)

        start_cmd = {
            "cmd": "start",
            "prompt": prompt,
            "cwd": cwd,
            "permission_mode": permission_mode,
            "resume": resume,
            "system_prompt": SYSTEM_PROMPT,
        }
        self._write_cmd(proc, start_cmd)

        # Launch reader as a background task
        task = asyncio.create_task(
            self._read_events(session_id, proc, cwd, permission_mode)
        )
        self._readers[session_id] = task

    def _write_cmd(self, proc: asyncio.subprocess.Process, cmd: dict) -> None:
        """Write a JSON-line command to the subprocess stdin."""
        if proc.stdin is None:
            return
        line = json.dumps(cmd, separators=(",", ":")) + "\n"
        proc.stdin.write(line.encode())
        try:
            # We don't await drain here to avoid blocking — the pipe buffer
            # is large enough for our small JSON commands.
            asyncio.ensure_future(proc.stdin.drain())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal: event reader
    # ------------------------------------------------------------------

    async def _read_events(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
        cwd: str | None,
        permission_mode: str,
    ) -> None:
        """Read JSON-line events from subprocess stdout and dispatch them."""
        start_time = time.monotonic()

        try:
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Malformed JSON from subprocess", raw=raw[:200])
                    continue
                await self._handle_event(session_id, proc, event)
        except asyncio.CancelledError:
            logger.info("Reader task cancelled", session_id=session_id)
        except Exception:
            logger.exception("Reader task failed", session_id=session_id)
            await self._events.on_error(session_id, "SUBPROCESS_READER_ERROR", "Reader task crashed")
        finally:
            # Wait for process to exit
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()

            # Read any remaining stderr for logging
            if proc.stderr:
                try:
                    stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
                    if stderr_data:
                        for line in stderr_data.decode(errors="replace").splitlines():
                            if line.strip():
                                if any(s in line for s in _IGNORED_WORKER_STDERR_SUBSTRINGS):
                                    continue
                                logger.debug("Worker stderr", session_id=session_id, line=line)
                except (asyncio.TimeoutError, Exception):
                    pass

            # Final heartbeat
            elapsed = time.monotonic() - start_time
            await self._events.on_heartbeat(session_id, elapsed, done=True)

            # Cleanup
            self._processes.pop(session_id, None)
            store.clear_process(session_id)

            # Process queued inputs
            pending = self._pending_inputs.get(session_id)
            if pending and not store.is_stop_requested(session_id):
                next_input = pending.pop(0)
                if not pending:
                    self._pending_inputs.pop(session_id, None)
                await self._spawn(
                    session_id,
                    next_input,
                    cwd,
                    permission_mode,
                    resume=self._sdk_sessions.get(session_id),
                )
                return

            # Signal completion
            if store.is_stop_requested(session_id):
                await self._events.on_exit(session_id, proc.returncode)
            else:
                await self._events.on_awaiting_input(session_id)

    # ------------------------------------------------------------------
    # Internal: event dispatch
    # ------------------------------------------------------------------

    async def _handle_event(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
        event: dict,
    ) -> None:
        """Dispatch a single parsed event from the subprocess."""
        etype = event.get("event")

        if etype == "init":
            await self._on_init(session_id, event)

        elif etype == "output":
            await self._on_output(session_id, event)

        elif etype == "result":
            await self._on_result(session_id, event)

        elif etype == "permission_request":
            await self._on_permission_request(session_id, proc, event)

        elif etype == "heartbeat":
            elapsed = event.get("elapsed_s", 0.0)
            await self._events.on_heartbeat(session_id, elapsed, done=False)

        elif etype == "error":
            code = event.get("code", "SUBPROCESS_ERROR")
            message = event.get("message", "Unknown error")
            await self._events.on_error(session_id, code, message)

        elif etype == "stderr":
            logger.debug("Worker stderr", session_id=session_id, line=event.get("line", ""))

    async def _on_init(self, session_id: str, event: dict) -> None:
        sdk_session_id = event.get("session_id")

        expected = self._sdk_sessions.get(session_id)
        if expected and sdk_session_id and expected != sdk_session_id:
            # Session mismatch: the old session expired and the SDK created a
            # new one.  Update both the in-memory cache AND the store binding
            # so subsequent turns resume the correct session.
            logger.info(
                "SDK created new session (old one expired) — updating binding",
                session_id=session_id,
                expected=expected,
                actual=sdk_session_id,
            )
            self._sdk_sessions[session_id] = sdk_session_id
            store.replace_runner_session_id(session_id, expected, sdk_session_id)
        elif sdk_session_id:
            self._sdk_sessions[session_id] = sdk_session_id
            store.set_runner_session_id(session_id, sdk_session_id)

        model = event.get("model", "claude")
        version = event.get("version", "")
        title = f"Claude Code{f' {version}' if version else ''}"
        await self._events.on_header(
            session_id,
            title=title,
            model=model,
            provider="Anthropic (OAuth, subprocess)",
        )

    async def _on_output(self, session_id: str, event: dict) -> None:
        """Handle output event — classify text blocks as step vs final."""
        blocks = event.get("blocks", [])
        text_blocks = [b for b in blocks if b.get("type") == "text"]
        has_tool_use = any(b.get("type") == "tool_use" for b in blocks)
        last_text_index = len(text_blocks) - 1

        text_index = 0
        for block in blocks:
            btype = block.get("type")

            if btype == "text":
                is_final_text = not has_tool_use and text_index == last_text_index
                await self._events.on_output(
                    session_id,
                    "combined",
                    block.get("text", ""),
                    kind="final" if is_final_text else "step",
                    is_final=is_final_text,
                )
                text_index += 1

            elif btype == "tool_use":
                tool_info = f"[tool: {block.get('name', 'unknown')}]"
                await self._events.on_output(
                    session_id, "combined", f"{tool_info}\n", kind="step", is_final=False,
                )

            elif btype == "tool_result":
                content = block.get("content", "")
                truncated = content[:500] + "..." if len(content) > 500 else content
                prefix = "[error] " if block.get("is_error") else "[result] "
                await self._events.on_output(
                    session_id, "combined", f"{prefix}{truncated}\n", kind="step", is_final=False,
                )

            elif btype == "thinking":
                thinking = block.get("thinking", "")
                if thinking:
                    await self._events.on_output(
                        session_id, "combined", f"[thinking] {thinking}\n", kind="step", is_final=False,
                    )

    async def _on_result(self, session_id: str, event: dict) -> None:
        input_tokens = event.get("input_tokens", 0)
        output_tokens = event.get("output_tokens", 0)
        if input_tokens or output_tokens:
            await self._events.on_metadata(
                session_id,
                "tokens",
                {"input": input_tokens, "output": output_tokens},
                f"input: {input_tokens}, output: {output_tokens}",
            )

        cost = event.get("cost_usd")
        if cost is not None:
            await self._events.on_metadata(
                session_id, "cost", cost, f"${cost:.4f}",
            )

        if event.get("is_error"):
            await self._events.on_error(
                session_id,
                "CLAUDE_RESULT_ERROR",
                event.get("error_text") or "Unknown error",
            )

    async def _on_permission_request(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
        event: dict,
    ) -> None:
        """Handle permission request from subprocess.

        Creates a Future in the store, emits the UI event, and spawns a
        background task that waits for the Future to resolve and then
        writes the response back to the subprocess stdin.
        """
        request_id = event.get("request_id", f"perm_{uuid.uuid4().hex[:12]}")
        tool_name = event.get("tool_name", "unknown")
        tool_input = event.get("tool_input", {})

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        store.add_pending_permission(session_id, request_id, tool_name, tool_input, future)

        await self._events.on_permission_request(
            session_id,
            request_id=request_id,
            tool_name=tool_name,
            tool_input=tool_input,
            suggestions=None,
        )

        async def _wait_and_respond() -> None:
            try:
                result = await asyncio.wait_for(future, timeout=PERMISSION_TIMEOUT)
                behavior = result.get("behavior", "deny")
            except asyncio.TimeoutError:
                behavior = "deny"
                store.resolve_pending_permission(
                    session_id, request_id, {"behavior": "deny", "message": "Timeout"}
                )
                await self._events.on_permission_resolved(
                    session_id,
                    request_id=request_id,
                    resolved_by="timeout",
                    allowed=False,
                    message="Permission request timed out",
                )
            except asyncio.CancelledError:
                behavior = "deny"
                await self._events.on_permission_resolved(
                    session_id,
                    request_id=request_id,
                    resolved_by="cancelled",
                    allowed=False,
                    message="Session was interrupted",
                )
                return

            self._write_cmd(proc, {
                "cmd": "permission_response",
                "request_id": request_id,
                "behavior": behavior,
            })

        asyncio.create_task(_wait_and_respond())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _map_permission_mode(approval_choice: int) -> str:
        if approval_choice == 2:
            return "bypassPermissions"
        elif approval_choice == 1:
            return "acceptEdits"
        else:
            return "default"

    def _maybe_drop_busy_resume(self, session_id: str, resume: str | None) -> str | None:
        """If the Claude session is running in another process, don't resume it.

        Resuming a running Claude Code session can fail hard (depending on CLI/SDK
        state). Starting a new session is more reliable and we can re-bind on
        the subsequent init event.

        We only drop the resume when the session is genuinely busy in an
        **external** process.  Our own managed sessions are already guarded by
        the ``send_input`` queuing logic, so we skip the check when the
        resume ID belongs to a session we own — this avoids false positives
        from brief process-cleanup races.
        """
        if not resume:
            return None

        # If we manage this tether session and the subprocess has exited,
        # the resume is safe — no need to hit ``ps``.
        proc = self._processes.get(session_id)
        if proc is not None and proc.returncode is not None:
            return resume

        try:
            if is_claude_session_running(resume):
                logger.warning(
                    "External Claude session appears busy; starting without resume",
                    session_id=session_id,
                    resume=resume,
                )
                return None
        except Exception:
            # Don't block session progress on best-effort detection.
            return resume
        return resume
