"""Runner adapter for the pi coding agent via JSON-RPC over stdin/stdout.

Spawns ``pi --mode rpc`` as a subprocess and translates pi's event stream
into Tether's ``RunnerEvents`` protocol.  Supports session resume by passing
the session file path.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import time
import uuid

import structlog

from tether.runner.base import RunnerEvents, RunnerUnavailableError
from tether.store import store

logger = structlog.get_logger(__name__)

HEARTBEAT_INTERVAL = 5.0
PERMISSION_TIMEOUT = 300.0

# Pi tool calls that should trigger permission requests in Tether
_PERMISSION_TOOLS = {"bash", "write", "edit"}


def _find_pi_binary() -> str | None:
    """Locate the pi binary on PATH or in common locations."""
    found = shutil.which("pi")
    if found:
        return found

    # Check common nvm/node locations when PATH doesn't include them
    # (e.g. when Tether is launched from an IDE or systemd)
    candidates = [
        os.path.expanduser("~/.nvm/versions/node/*/bin/pi"),
        "/usr/local/bin/pi",
        "/usr/bin/pi",
        os.path.expanduser("~/.local/bin/pi"),
        os.path.expanduser("~/.npm-global/bin/pi"),
    ]
    for pattern in candidates:
        matches = glob.glob(pattern)
        if matches:
            # Pick the latest version if multiple nvm versions exist
            matches.sort(reverse=True)
            return matches[0]

    return None


class PiRpcRunner:
    """Runner that communicates with pi via its JSON-RPC mode."""

    runner_type: str = "pi"

    def __init__(self, events: RunnerEvents) -> None:
        self._events = events
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._readers: dict[str, asyncio.Task] = {}
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}
        self._session_files: dict[str, str] = {}  # tether session_id -> pi session file
        self._pending_inputs: dict[str, list[str]] = {}
        self._is_streaming: dict[str, bool] = {}
        self._pi_binary: str | None = None

    # ------------------------------------------------------------------
    # Runner protocol
    # ------------------------------------------------------------------

    async def start(self, session_id: str, prompt: str, approval_choice: int) -> None:
        logger.info(
            "Starting pi_rpc session",
            session_id=session_id,
            approval_choice=approval_choice,
        )
        store.clear_stop_requested(session_id)

        session = store.get_session(session_id)
        cwd = session.directory if session and session.directory else None

        # Look for an existing pi session file to resume
        session_file = self._session_files.get(session_id)
        if not session_file:
            runner_sid = store.get_runner_session_id(session_id)
            if runner_sid:
                # runner_session_id stores the pi session UUID; find the file
                from tether.discovery.pi_sessions import _find_session_file

                path = _find_session_file(runner_sid)
                if path:
                    session_file = str(path)
                    self._session_files[session_id] = session_file

        await self._spawn(session_id, cwd, session_file)
        await self._send_prompt(session_id, prompt)

    async def send_input(self, session_id: str, text: str) -> None:
        if not text.strip():
            return

        proc = self._processes.get(session_id)
        if not proc or proc.returncode is not None:
            # No running process — need to respawn
            session = store.get_session(session_id)
            cwd = session.directory if session and session.directory else None

            # Look for session file (may have been attached externally)
            session_file = self._session_files.get(session_id)
            if not session_file:
                runner_sid = store.get_runner_session_id(session_id)
                if runner_sid:
                    from tether.discovery.pi_sessions import _find_session_file

                    path = _find_session_file(runner_sid)
                    if path:
                        session_file = str(path)
                        self._session_files[session_id] = session_file

            store.clear_stop_requested(session_id)
            await self._spawn(session_id, cwd, session_file)
            await self._send_prompt(session_id, text)
            return

        if self._is_streaming.get(session_id):
            # Agent is busy — queue as follow-up
            self._write_cmd(proc, {
                "type": "follow_up",
                "message": text,
            })
        else:
            await self._send_prompt(session_id, text)

    async def stop(self, session_id: str) -> int | None:
        store.request_stop(session_id)
        store.clear_pending_permissions(session_id)

        proc = self._processes.get(session_id)
        if proc and proc.returncode is None:
            # Send abort
            self._write_cmd(proc, {"type": "abort"})
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "Pi process did not exit in time, killing",
                    session_id=session_id,
                )
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

        self._cleanup(session_id)
        store.clear_stop_requested(session_id)
        return 0

    def update_permission_mode(self, session_id: str, approval_choice: int) -> None:
        logger.info(
            "Updated permission mode (pi uses tool_call events for permissions)",
            session_id=session_id,
            approval_choice=approval_choice,
        )

    # ------------------------------------------------------------------
    # Internal: subprocess lifecycle
    # ------------------------------------------------------------------

    async def _spawn(
        self,
        session_id: str,
        cwd: str | None,
        session_file: str | None,
    ) -> None:
        """Spawn a ``pi --mode rpc`` subprocess."""
        pi_bin = self._get_pi_binary()

        args = [pi_bin, "--mode", "rpc"]
        if session_file:
            args.extend(["--session", session_file])
        else:
            args.append("--no-session")

        logger.info(
            "Spawning pi process",
            session_id=session_id,
            args=args,
            cwd=cwd,
            session_file=session_file,
        )

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            limit=10 * 1024 * 1024,  # 10MB buffer for large tool outputs
        )
        self._processes[session_id] = proc
        self._is_streaming[session_id] = False
        store.set_process(session_id, proc)

        # Start event reader
        task = asyncio.create_task(self._read_events(session_id, proc))
        self._readers[session_id] = task

        # Start heartbeat
        hb_task = asyncio.create_task(self._heartbeat_loop(session_id, proc))
        self._heartbeat_tasks[session_id] = hb_task

        # Emit header
        await self._events.on_header(
            session_id,
            title="Pi Coding Agent",
            model="unknown",
            provider="pi (RPC)",
        )

        # Fetch initial state for model info
        self._write_cmd(proc, {"type": "get_state"})

    async def _send_prompt(self, session_id: str, text: str) -> None:
        """Send a prompt to the pi process."""
        proc = self._processes.get(session_id)
        if not proc or proc.returncode is not None:
            logger.warning("No pi process to send prompt to", session_id=session_id)
            return

        logger.info("Sending prompt to pi", session_id=session_id, text_length=len(text))
        self._write_cmd(proc, {
            "type": "prompt",
            "message": text,
        })

    def _write_cmd(self, proc: asyncio.subprocess.Process, cmd: dict) -> None:
        """Write a JSON-line command to the subprocess stdin."""
        if proc.stdin is None:
            return
        line = json.dumps(cmd, separators=(",", ":")) + "\n"
        proc.stdin.write(line.encode())
        try:
            asyncio.ensure_future(proc.stdin.drain())
        except Exception:
            pass

    def _get_pi_binary(self) -> str:
        """Find the pi binary, raising if not available."""
        if self._pi_binary:
            return self._pi_binary
        pi_bin = _find_pi_binary()
        if not pi_bin:
            raise RunnerUnavailableError(
                "pi binary not found. Install with: npm install -g @mariozechner/pi-coding-agent"
            )
        self._pi_binary = pi_bin
        return pi_bin

    def _cleanup(self, session_id: str) -> None:
        """Clean up all state for a session."""
        # Cancel reader
        reader = self._readers.pop(session_id, None)
        if reader and not reader.done():
            reader.cancel()

        # Cancel heartbeat
        hb = self._heartbeat_tasks.pop(session_id, None)
        if hb and not hb.done():
            hb.cancel()

        self._processes.pop(session_id, None)
        self._pending_inputs.pop(session_id, None)
        self._is_streaming.pop(session_id, False)
        store.clear_process(session_id)

    # ------------------------------------------------------------------
    # Internal: heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(
        self, session_id: str, proc: asyncio.subprocess.Process
    ) -> None:
        """Send periodic heartbeats while the process is alive."""
        start_time = time.monotonic()
        try:
            while proc.returncode is None:
                elapsed = time.monotonic() - start_time
                await self._events.on_heartbeat(session_id, elapsed, done=False)
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Internal: event reader
    # ------------------------------------------------------------------

    async def _read_events(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """Read JSON-line events from pi's stdout and dispatch them."""
        start_time = time.monotonic()

        logger.info("Starting pi event reader", session_id=session_id)
        try:
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    logger.info("Pi stdout EOF", session_id=session_id)
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug(
                        "Non-JSON output from pi",
                        session_id=session_id,
                        raw=raw[:200],
                    )
                    continue
                await self._handle_event(session_id, proc, event)
        except asyncio.CancelledError:
            logger.info("Pi reader task cancelled", session_id=session_id)
        except Exception:
            logger.exception("Pi reader task failed", session_id=session_id)
            await self._events.on_error(
                session_id, "PI_READER_ERROR", "Reader task crashed"
            )
        finally:
            # Wait for process exit
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()

            # Read stderr
            if proc.stderr:
                try:
                    stderr_data = await asyncio.wait_for(
                        proc.stderr.read(), timeout=2.0
                    )
                    if stderr_data:
                        for line in stderr_data.decode(errors="replace").splitlines():
                            if line.strip():
                                logger.debug(
                                    "Pi stderr",
                                    session_id=session_id,
                                    line=line,
                                )
                except (asyncio.TimeoutError, Exception):
                    pass

            # Final heartbeat
            elapsed = time.monotonic() - start_time
            await self._events.on_heartbeat(session_id, elapsed, done=True)

            # Cleanup
            self._cleanup(session_id)

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
        """Dispatch a single parsed event from pi's RPC output."""
        etype = event.get("type")

        # -- Responses to commands --
        if etype == "response":
            await self._handle_response(session_id, event)
            return

        # -- Agent lifecycle --
        if etype == "agent_start":
            self._is_streaming[session_id] = True

        elif etype == "agent_end":
            self._is_streaming[session_id] = False
            # Emit final accumulated text from agent_end messages if available
            messages = event.get("messages", [])
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    content = msg.get("content", [])
                    for block in content if isinstance(content, list) else []:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                await self._events.on_output(
                                    session_id,
                                    "combined",
                                    text,
                                    kind="final",
                                    is_final=True,
                                )

        # -- Streaming text --
        elif etype == "message_update":
            delta_event = event.get("assistantMessageEvent", {})
            delta_type = delta_event.get("type")

            if delta_type == "text_delta":
                delta = delta_event.get("delta", "")
                if delta:
                    await self._events.on_output(
                        session_id,
                        "combined",
                        delta,
                        kind="step",
                        is_final=False,
                    )

            elif delta_type == "thinking_delta":
                delta = delta_event.get("delta", "")
                if delta:
                    await self._events.on_output(
                        session_id,
                        "combined",
                        f"[thinking] {delta}",
                        kind="step",
                        is_final=False,
                    )

            elif delta_type == "done":
                # Message complete — the agent_end event will carry the final text
                pass

            elif delta_type == "error":
                reason = delta_event.get("reason", "unknown")
                await self._events.on_error(
                    session_id, "PI_STREAM_ERROR", f"Stream error: {reason}"
                )

        # -- Tool execution --
        elif etype == "tool_execution_start":
            tool_name = event.get("toolName", "unknown")
            args = event.get("args", {})

            await self._events.on_output(
                session_id,
                "combined",
                f"[tool: {tool_name}]\n",
                kind="step",
                is_final=False,
            )

            # For write/edit/bash, emit permission request
            if tool_name in _PERMISSION_TOOLS:
                request_id = event.get("toolCallId", f"pi_{uuid.uuid4().hex[:12]}")

                loop = asyncio.get_running_loop()
                future: asyncio.Future = loop.create_future()

                # NOTE: Pi auto-approves tools by default (like the TUI does).
                # The requiresApproval flag is informational only.
                # We auto-resolve immediately without showing UI prompts.
                store.add_pending_permission(
                    session_id, request_id, tool_name, args, future
                )
                
                # Auto-resolve immediately (don't emit permission_request to UI)
                store.resolve_pending_permission(
                    session_id, request_id, {"behavior": "allow"}
                )
                
                # Emit resolved event for logging/tracking
                await self._events.on_permission_resolved(
                    session_id,
                    request_id=request_id,
                    resolved_by="auto",
                    allowed=True,
                )

        elif etype == "tool_execution_update":
            tool_name = event.get("toolName", "unknown")
            partial = event.get("partialResult", {})
            content = partial.get("content", [])
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        await self._events.on_output(
                            session_id,
                            "combined",
                            f"[{tool_name}] {text}\n",
                            kind="step",
                            is_final=False,
                        )

        elif etype == "tool_execution_end":
            tool_name = event.get("toolName", "unknown")
            is_error = event.get("isError", False)
            result = event.get("result", {})
            content = result.get("content", [])
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            text = "\n".join(text_parts)

            if text:
                truncated = text[:500] + "..." if len(text) > 500 else text
                prefix = "[error] " if is_error else "[result] "
                await self._events.on_output(
                    session_id,
                    "combined",
                    f"{prefix}{truncated}\n",
                    kind="step",
                    is_final=False,
                )

        # -- Compaction --
        elif etype == "auto_compaction_start":
            await self._events.on_output(
                session_id,
                "combined",
                "[compacting context...]\n",
                kind="step",
                is_final=False,
            )

        elif etype == "auto_compaction_end":
            result = event.get("result")
            if result:
                tokens_before = result.get("tokensBefore", 0)
                await self._events.on_output(
                    session_id,
                    "combined",
                    f"[compaction done — was {tokens_before} tokens]\n",
                    kind="step",
                    is_final=False,
                )

        # -- Retry --
        elif etype == "auto_retry_start":
            attempt = event.get("attempt", 0)
            max_attempts = event.get("maxAttempts", 0)
            delay_ms = event.get("delayMs", 0)
            await self._events.on_output(
                session_id,
                "combined",
                f"[retry {attempt}/{max_attempts}, waiting {delay_ms}ms...]\n",
                kind="step",
                is_final=False,
            )

        elif etype == "auto_retry_end":
            success = event.get("success", False)
            if not success:
                error = event.get("finalError", "Unknown")
                await self._events.on_error(
                    session_id, "PI_RETRY_FAILED", f"Retry failed: {error}"
                )

        # -- Extension UI requests (fire-and-forget, we log them) --
        elif etype == "extension_ui_request":
            method = event.get("method")
            if method == "notify":
                msg = event.get("message", "")
                if msg:
                    await self._events.on_output(
                        session_id,
                        "combined",
                        f"[notify] {msg}\n",
                        kind="step",
                        is_final=False,
                    )

    async def _handle_response(self, session_id: str, event: dict) -> None:
        """Handle a command response from pi."""
        command = event.get("command", "")
        success = event.get("success", False)

        if not success:
            error = event.get("error", "Unknown error")
            logger.warning(
                "Pi command failed",
                session_id=session_id,
                command=command,
                error=error,
            )
            if command == "prompt":
                await self._events.on_error(
                    session_id, "PI_PROMPT_ERROR", f"Prompt failed: {error}"
                )

        if command == "get_state":
            data = event.get("data", {})
            model_info = data.get("model")
            if isinstance(model_info, dict):
                model_name = model_info.get("name", "unknown")
                model_id = model_info.get("id", "unknown")
                provider = model_info.get("provider", "unknown")
                await self._events.on_header(
                    session_id,
                    title=f"Pi — {model_name}",
                    model=model_id,
                    provider=provider,
                )

            session_file = data.get("sessionFile")
            if session_file:
                self._session_files[session_id] = session_file
                # Extract session UUID from the file path
                import re

                uuid_match = re.search(
                    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                    session_file,
                )
                if uuid_match:
                    pi_session_id = uuid_match.group(1)
                    self._session_files[session_id] = session_file
                    store.set_runner_session_id(session_id, pi_session_id)
