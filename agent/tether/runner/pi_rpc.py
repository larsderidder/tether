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
import re
import shutil
import time
import uuid
from pathlib import Path

import structlog

from tether.discovery.pi_sessions import (
    _find_session_file,
    get_pi_session_detail,
    get_pi_session_model,
)
from tether.models import SessionState
from tether.runner.base import RunnerEvents, RunnerUnavailableError
from tether.settings import settings
from tether.store import store

logger = structlog.get_logger(__name__)

HEARTBEAT_INTERVAL = 5.0
PERMISSION_TIMEOUT = 300.0
_PI_RPC_STREAM_LIMIT_BYTES = 100 * 1024 * 1024
_PI_RESUME_MAX_SESSION_FILE_BYTES = 150 * 1024 * 1024
_TOOL_OUTPUT_MAX_CHARS = 1200
_TOOL_OUTPUT_MAX_LINES = 80
_TETHER_SESSION_NAME_PREFIX = "Tether: "
_TETHER_SESSION_NAME_MAX = 80
_CONTEXT_WARNING_PERCENT = 80.0

# Pi tool calls that should trigger permission requests in Tether
_PERMISSION_TOOLS = {"bash", "write", "edit"}


def _truncate_tool_output(
    text: str,
    *,
    char_limit: int | None = None,
    line_limit: int | None = None,
) -> str:
    """Keep tool output readable in chat surfaces."""

    char_limit = char_limit or settings.pi_tool_output_max_chars()
    line_limit = line_limit or settings.pi_tool_output_max_lines()
    lines = text.splitlines()
    line_truncated = len(lines) > line_limit
    preview = "\n".join(lines[:line_limit] if line_truncated else lines)

    char_truncated = len(preview) > char_limit
    if char_truncated:
        preview = preview[:char_limit].rstrip()

    notes: list[str] = []
    if line_truncated:
        notes.append(f"{len(lines) - line_limit:,} more lines")
    if char_truncated:
        notes.append("additional characters")

    if not notes:
        return text
    return f"{preview.rstrip()}\n\n[truncated, {' and '.join(notes)} omitted]"


def _bridge_segment(
    kind: str, text: str = "", label: str | None = None
) -> list[dict[str, str]]:
    """Build a serialized bridge output segment."""

    segment = {"kind": kind, "text": text}
    if label:
        segment["label"] = label
    return [segment]


def _tether_session_name(name: str | None) -> str | None:
    """Return an initial pi session name that identifies Tether-owned sessions."""
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        return None
    if cleaned.casefold().startswith(_TETHER_SESSION_NAME_PREFIX.casefold()):
        return cleaned[:_TETHER_SESSION_NAME_MAX]
    return f"{_TETHER_SESSION_NAME_PREFIX}{cleaned}"[:_TETHER_SESSION_NAME_MAX]


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
        self._streamed_text: dict[str, bool] = (
            {}
        )  # True if text_delta events were received
        self._tool_had_updates: dict[str, set[str]] = (
            {}
        )  # tool_call_ids with streamed output
        self._assistant_marker_needed: dict[str, bool] = {}
        self._thinking_marker_needed: dict[str, bool] = {}
        self._at_line_start: dict[str, bool] = {}
        self._context_windows: dict[str, int] = {}
        self._auto_compaction_enabled: dict[str, bool] = {}
        self._context_warning_sent: set[str] = set()
        self._pi_binary: str | None = None

    # ------------------------------------------------------------------
    # Runner protocol
    # ------------------------------------------------------------------

    async def start(
        self,
        session_id: str,
        prompt: str,
        approval_choice: int,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        logger.info(
            "Starting pi_rpc session",
            session_id=session_id,
            approval_choice=approval_choice,
            image_count=len(images or []),
        )
        store.clear_stop_requested(session_id)

        session = store.get_session(session_id)
        cwd = session.directory if session and session.directory else None
        model = session.model if session and session.model else None

        session_file = await self._resolve_session_file(session_id)

        await self._spawn(session_id, cwd, session_file, model=model)
        await self._send_prompt(session_id, prompt, images=images)

    async def send_input(
        self,
        session_id: str,
        text: str,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        if not text.strip() and not images:
            return

        proc = self._processes.get(session_id)
        if not proc or proc.returncode is not None:
            # No running process — need to respawn
            session = store.get_session(session_id)
            cwd = session.directory if session and session.directory else None
            model = session.model if session and session.model else None

            session_file = await self._resolve_session_file(session_id)

            store.clear_stop_requested(session_id)
            await self._spawn(session_id, cwd, session_file, model=model)
            await self._send_prompt(session_id, text, images=images)
            return

        if self._is_streaming.get(session_id):
            # Agent is busy; queue as follow-up.
            await self._write_cmd_async(
                proc,
                {
                    "type": "follow_up",
                    "message": text,
                    "images": images or [],
                },
            )
        else:
            await self._send_prompt(session_id, text, images=images)

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

    async def compact(
        self,
        session_id: str,
        custom_instructions: str | None = None,
    ) -> None:
        """Request manual compaction from pi RPC."""

        proc = self._processes.get(session_id)
        if not proc or proc.returncode is not None:
            session = store.get_session(session_id)
            cwd = session.directory if session and session.directory else None
            session_file = await self._resolve_session_file(
                session_id,
                enforce_size_limit=False,
            )
            model = session.model if session and session.model else None
            store.clear_stop_requested(session_id)
            await self._spawn(session_id, cwd, session_file, model=model)
            proc = self._processes.get(session_id)

        if not proc or proc.returncode is not None:
            raise RunnerUnavailableError("pi process is not available")

        command = {"type": "compact"}
        if custom_instructions:
            command["customInstructions"] = custom_instructions
        await self._write_cmd_async(proc, command)

    async def _resolve_session_file(
        self,
        session_id: str,
        *,
        enforce_size_limit: bool = True,
    ) -> str | None:
        """Find a resumable pi session file, unless it is too large to send."""

        session_file = self._session_files.get(session_id)
        if not session_file:
            runner_sid = store.get_runner_session_id(session_id)
            if runner_sid:
                path = _find_session_file(runner_sid)
                if path:
                    session_file = str(path)
                    self._session_files[session_id] = session_file

        if not session_file:
            return None

        try:
            size = Path(session_file).stat().st_size
        except OSError:
            logger.warning(
                "Pi session file is not readable, starting fresh",
                session_id=session_id,
                session_file=session_file,
            )
            self._session_files.pop(session_id, None)
            return None

        max_size = settings.pi_resume_max_session_file_bytes()
        if not enforce_size_limit or size <= max_size:
            return session_file

        size_mb = size / 1024 / 1024
        max_mb = max_size / 1024 / 1024
        message = (
            f"Pi session history is too large to resume ({size_mb:.0f} MB, "
            f"limit {max_mb:.0f} MB). The session binding was kept. "
            "Start a fresh session or compact the pi history before resuming."
        )
        logger.warning(
            "Pi session file is too large to resume",
            session_id=session_id,
            session_file=session_file,
            size_bytes=size,
            max_bytes=max_size,
        )
        raise RunnerUnavailableError(message)

    # ------------------------------------------------------------------
    # Internal: subprocess lifecycle
    # ------------------------------------------------------------------

    async def _spawn(
        self,
        session_id: str,
        cwd: str | None,
        session_file: str | None,
        model: str | None = None,
    ) -> None:
        """Spawn a ``pi --mode rpc`` subprocess."""
        pi_bin = self._get_pi_binary()

        args = [pi_bin, "--mode", "rpc"]
        if not session_file:
            session = store.get_session(session_id)
            session_name = _tether_session_name(
                getattr(session, "name", None) if session else None
            )
            if session_name:
                args.extend(["--name", session_name])
        if session_file:
            args.extend(["--session", session_file])
            # Pass the session's model explicitly so pi uses it regardless of
            # any scoped models (--models / enabledModels) configured in the
            # user's pi settings. Without this, pi's buildSessionOptions picks
            # the first scoped model as the default, overriding the session's
            # model before sdk.js gets a chance to restore it.
            session_model = get_pi_session_model(Path(session_file))
            if session_model and not model:
                provider, model_id = session_model
                model = f"{provider}/{model_id}"
        if model:
            args.extend(["--model", model])
            logger.info(
                "Passing configured model to pi",
                session_id=session_id,
                model=model,
            )

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
            limit=_PI_RPC_STREAM_LIMIT_BYTES,
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
        await self._write_cmd_async(proc, {"type": "get_state"})

    async def _send_prompt(
        self,
        session_id: str,
        text: str,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        """Send a prompt to the pi process."""
        proc = self._processes.get(session_id)
        if not proc or proc.returncode is not None:
            logger.warning("No pi process to send prompt to", session_id=session_id)
            return

        logger.info(
            "Sending prompt to pi",
            session_id=session_id,
            text_length=len(text),
            image_count=len(images or []),
        )
        await self._write_cmd_async(
            proc,
            {
                "type": "prompt",
                "message": text,
                "images": images or [],
                "streamingBehavior": "followUp",
            },
        )

    def _write_cmd(self, proc: asyncio.subprocess.Process, cmd: dict) -> None:
        """Write a JSON-line command to the subprocess stdin (sync version)."""
        if proc.stdin is None:
            return
        line = json.dumps(cmd, separators=(",", ":")) + "\n"
        proc.stdin.write(line.encode())
        # Note: This doesn't await drain(). Use _write_cmd_async for async contexts.

    async def _write_cmd_async(
        self, proc: asyncio.subprocess.Process, cmd: dict
    ) -> None:
        """Write a JSON-line command to the subprocess stdin and await flush."""
        if proc.stdin is None:
            return
        line = json.dumps(cmd, separators=(",", ":")) + "\n"
        proc.stdin.write(line.encode())
        try:
            await proc.stdin.drain()
        except Exception:
            logger.debug("Failed to drain stdin", exc_info=True)

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
        self._streamed_text.pop(session_id, False)
        self._tool_had_updates.pop(session_id, None)
        self._assistant_marker_needed.pop(session_id, None)
        self._thinking_marker_needed.pop(session_id, None)
        self._at_line_start.pop(session_id, None)
        self._context_windows.pop(session_id, None)
        self._auto_compaction_enabled.pop(session_id, None)
        self._context_warning_sent.discard(session_id)
        store.clear_process(session_id)

    async def _emit_output(
        self,
        session_id: str,
        stream: str,
        text: str,
        *,
        kind: str,
        is_final: bool,
        bridge_segments: list[dict[str, str]] | None = None,
    ) -> None:
        await self._events.on_output(
            session_id,
            stream,
            text,
            kind=kind,
            is_final=is_final,
            bridge_segments=bridge_segments,
        )
        self._at_line_start[session_id] = text.endswith("\n")

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

                # Strip terminal escape sequences (e.g., ]777;notify;...)
                # Pi emits OSC notifications before agent_end JSON
                line = raw.decode() if isinstance(raw, bytes) else raw
                if "]777;notify;" in line:
                    # Find the start of JSON (after the notification)
                    json_start = line.find('{"type":')
                    if json_start > 0:
                        line = line[json_start:]

                try:
                    event = json.loads(line)
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
        if etype in {"agent_start", "agent_end"}:
            await self._handle_agent_lifecycle_event(session_id, event)
            return

        # -- Streaming text --
        elif etype == "message_update":
            await self._handle_message_update(session_id, event)
            return
        elif etype == "turn_end":
            await self._handle_turn_end(session_id, event)
            return

        # -- Tool execution --
        elif etype in {
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
        }:
            await self._handle_tool_execution_event(session_id, event)
            return

        # -- Compaction --
        elif etype in {
            "auto_compaction_start",
            "compaction_start",
            "auto_compaction_end",
            "compaction_end",
        }:
            await self._handle_compaction_event(session_id, event)
            return

        # -- Retry --
        elif etype in {"auto_retry_start", "auto_retry_end"}:
            await self._handle_retry_event(session_id, event)
            return

        # -- Extension UI requests --
        elif etype == "extension_ui_request":
            await self._handle_extension_ui_request(session_id, event)

    async def _handle_agent_lifecycle_event(self, session_id: str, event: dict) -> None:
        """Handle agent start and end events from pi."""
        etype = event.get("type")
        if etype == "agent_start":
            self._is_streaming[session_id] = True
            self._streamed_text[session_id] = False
            self._assistant_marker_needed[session_id] = False
            self._thinking_marker_needed[session_id] = True
            self._at_line_start[session_id] = True
            return

        if etype != "agent_end":
            return

        self._is_streaming.pop(session_id, False)
        self._streamed_text.pop(session_id, False)
        emitted_final = await self._emit_agent_final_messages(session_id, event)
        agent_error = self._agent_end_error_message(event)
        if emitted_final or agent_error:
            await self._advance_external_sync_cursor(session_id)
        if agent_error:
            if event.get("willRetry"):
                return
            can_auto_compact = self._auto_compaction_enabled.get(session_id, False)
            if self._is_context_overflow_error(agent_error) and can_auto_compact:
                await self._emit_context_recovery_notice(session_id)
                return
            if self._is_unrecoverable_agent_error(agent_error):
                await self._discard_unrecoverable_pi_session(session_id)
            await self._events.on_error(session_id, "PI_AGENT_ERROR", agent_error)
            return

        # The pi process stays alive between turns, so _read_events will not
        # signal completion until the process exits.
        await self._events.on_awaiting_input(session_id)

    @staticmethod
    def _is_context_overflow_error(message: str) -> bool:
        """Return true for provider errors that Pi can recover via compaction."""
        normalized = message.casefold()
        return "context window" in normalized and any(
            marker in normalized
            for marker in ("exceed", "maximum", "too large", "too long")
        )

    @staticmethod
    def _is_unrecoverable_agent_error(message: str) -> bool:
        """Return true for pi history errors that poison resumed sessions."""
        return "no tool call found for function call output" in message.casefold()

    async def _emit_context_recovery_notice(self, session_id: str) -> None:
        """Tell bridge users that Pi is compacting and will continue the turn."""
        message = "Pi reached the context limit. It is compacting context and will retry the turn."
        await self._emit_output(
            session_id,
            "combined",
            f"[warning] {message}\n",
            kind="step",
            is_final=False,
            bridge_segments=_bridge_segment("warning", message),
        )

    async def _discard_unrecoverable_pi_session(self, session_id: str) -> None:
        """Forget a poisoned pi session so the next input starts fresh."""
        self._session_files.pop(session_id, None)
        store.clear_runner_session_id(session_id, force=True)
        proc = self._processes.get(session_id)
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            except Exception:
                logger.exception(
                    "Failed to terminate poisoned pi process", session_id=session_id
                )
        await self._emit_output(
            session_id,
            "combined",
            "[notify] Pi session history is corrupted; the next input will start a fresh pi context.\n",
            kind="step",
            is_final=False,
            bridge_segments=_bridge_segment(
                "status",
                "Pi session history is corrupted; the next input will start a fresh pi context.",
            ),
        )

    @staticmethod
    def _agent_end_error_message(event: dict) -> str | None:
        """Extract a visible pi agent error from an agent_end event."""
        top_level_error = event.get("errorMessage") or event.get("finalError")
        if top_level_error:
            return str(top_level_error)

        for msg in event.get("messages", []):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            error_message = msg.get("errorMessage")
            if error_message:
                return str(error_message)
            if msg.get("stopReason") == "error":
                return "Pi agent stopped with an error before producing a response."
        return None

    async def _emit_agent_final_messages(self, session_id: str, event: dict) -> bool:
        """Emit assistant text blocks from an agent_end event."""
        messages = event.get("messages", [])
        emitted_final = False
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text", "")
                if not text:
                    continue
                prefix = ""
                if self._assistant_marker_needed.get(session_id):
                    lead = "" if self._at_line_start.get(session_id, True) else "\n"
                    prefix = f"{lead}[assistant] "
                await self._emit_output(
                    session_id,
                    "combined",
                    f"{prefix}{text}",
                    kind="final",
                    is_final=True,
                    bridge_segments=_bridge_segment("assistant", text),
                )
                self._assistant_marker_needed[session_id] = False
                self._thinking_marker_needed[session_id] = True
                emitted_final = True
        return emitted_final

    async def _handle_turn_end(self, session_id: str, event: dict) -> None:
        """Warn once when a Pi turn reports high context usage."""
        if session_id in self._context_warning_sent:
            return

        context_window = self._context_windows.get(session_id, 0)
        message = event.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if context_window <= 0 or not isinstance(usage, dict):
            return

        # ASVS 2.2.1: accept only bounded positive numeric usage from the RPC process.
        try:
            context_tokens = int(usage.get("totalTokens") or 0)
        except (TypeError, ValueError, OverflowError):
            return
        if context_tokens <= 0 or context_tokens > context_window * 10:
            return

        percent = context_tokens / context_window * 100
        if percent < _CONTEXT_WARNING_PERCENT:
            return

        self._context_warning_sent.add(session_id)
        rounded_percent = min(999, round(percent))
        compaction_note = (
            "Pi auto-compaction is enabled."
            if self._auto_compaction_enabled.get(session_id, False)
            else "Run /compact soon to avoid losing the turn."
        )
        warning = (
            f"Pi context is {rounded_percent}% full "
            f"({context_tokens:,} of {context_window:,} tokens). {compaction_note}"
        )
        await self._emit_output(
            session_id,
            "combined",
            f"[warning] {warning}\n",
            kind="step",
            is_final=False,
            bridge_segments=_bridge_segment("warning", warning),
        )

    async def _handle_message_update(self, session_id: str, event: dict) -> None:
        """Handle streaming assistant message deltas from pi."""
        delta_event = event.get("assistantMessageEvent", {})
        delta_type = delta_event.get("type")

        if delta_type == "text_delta":
            await self._handle_text_delta(session_id, delta_event)
        elif delta_type == "thinking_delta":
            await self._handle_thinking_delta(session_id, delta_event)
        elif delta_type == "done":
            return
        elif delta_type == "error":
            logger.info(
                "Pi stream attempt reported an error; waiting for agent_end",
                session_id=session_id,
                reason=str(delta_event.get("reason", "unknown"))[:200],
            )

    async def _handle_text_delta(self, session_id: str, delta_event: dict) -> None:
        """Emit an assistant text delta."""
        delta = delta_event.get("delta", "")
        if not delta:
            return
        self._streamed_text[session_id] = True
        prefix = ""
        if self._assistant_marker_needed.get(session_id):
            lead = "" if self._at_line_start.get(session_id, True) else "\n"
            prefix = f"{lead}[assistant] "
        await self._emit_output(
            session_id,
            "combined",
            f"{prefix}{delta}",
            kind="step",
            is_final=False,
            bridge_segments=_bridge_segment("assistant", delta),
        )
        self._assistant_marker_needed[session_id] = False
        self._thinking_marker_needed[session_id] = True

    async def _handle_thinking_delta(self, session_id: str, delta_event: dict) -> None:
        """Emit a thinking delta."""
        delta = delta_event.get("delta", "")
        if not delta:
            return
        prefix = ""
        if self._thinking_marker_needed.get(session_id, True):
            lead = "" if self._at_line_start.get(session_id, True) else "\n"
            prefix = f"{lead}[thinking] "
        await self._emit_output(
            session_id,
            "combined",
            f"{prefix}{delta}",
            kind="step",
            is_final=False,
            bridge_segments=_bridge_segment("thinking", delta),
        )
        self._assistant_marker_needed[session_id] = True
        self._thinking_marker_needed[session_id] = False

    async def _handle_tool_execution_event(self, session_id: str, event: dict) -> None:
        """Handle tool start, streamed output, and completion events."""
        etype = event.get("type")
        if etype == "tool_execution_start":
            await self._handle_tool_execution_start(session_id, event)
        elif etype == "tool_execution_update":
            await self._handle_tool_execution_update(session_id, event)
        elif etype == "tool_execution_end":
            await self._handle_tool_execution_end(session_id, event)

    async def _handle_tool_execution_start(self, session_id: str, event: dict) -> None:
        """Emit a tool call and auto-resolve pi tool permissions."""
        tool_name = event.get("toolName", "unknown")
        args = event.get("args", {})

        await self._emit_output(
            session_id,
            "combined",
            f"[tool: {tool_name}]\n",
            kind="step",
            is_final=False,
            bridge_segments=_bridge_segment(
                "tool_call",
                json.dumps(args, ensure_ascii=False) if args else "",
                str(tool_name),
            ),
        )
        self._assistant_marker_needed[session_id] = True

        if tool_name not in _PERMISSION_TOOLS:
            return

        request_id = event.get("toolCallId", f"pi_{uuid.uuid4().hex[:12]}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        store.add_pending_permission(session_id, request_id, tool_name, args, future)
        store.resolve_pending_permission(session_id, request_id, {"behavior": "allow"})
        await self._events.on_permission_resolved(
            session_id,
            request_id=request_id,
            resolved_by="auto",
            allowed=True,
        )

    async def _handle_tool_execution_update(self, session_id: str, event: dict) -> None:
        """Emit streamed tool output without updating line-start state."""
        tool_name = event.get("toolName", "unknown")
        partial = event.get("partialResult", {})
        content = partial.get("content", [])
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    self._tool_had_updates.setdefault(session_id, set()).add(
                        event.get("toolCallId", "")
                    )
                    truncated = _truncate_tool_output(text)
                    await self._events.on_output(
                        session_id,
                        "combined",
                        f"[{tool_name}] {truncated}\n",
                        kind="step",
                        is_final=False,
                        bridge_segments=_bridge_segment(
                            "tool_output",
                            truncated,
                            str(tool_name),
                        ),
                    )
                    self._assistant_marker_needed[session_id] = True

    async def _handle_tool_execution_end(self, session_id: str, event: dict) -> None:
        """Emit final tool result output when pi did not stream it already."""
        tool_call_id = event.get("toolCallId", "")
        already_streamed = tool_call_id in self._tool_had_updates.get(session_id, set())
        if session_id in self._tool_had_updates:
            self._tool_had_updates[session_id].discard(tool_call_id)

        tool_name = event.get("toolName", "unknown")
        is_error = event.get("isError", False)
        result = event.get("result", {})
        content = result.get("content", [])
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        text = "\n".join(text_parts)

        if not text or (already_streamed and not is_error):
            return

        truncated = _truncate_tool_output(text)
        prefix = "[error] " if is_error else "[result] "
        await self._emit_output(
            session_id,
            "combined",
            f"{prefix}{truncated}\n",
            kind="step",
            is_final=False,
            bridge_segments=_bridge_segment(
                "tool_error" if is_error else "tool_result",
                truncated,
                str(tool_name),
            ),
        )
        self._assistant_marker_needed[session_id] = True

    async def _handle_compaction_event(self, session_id: str, event: dict) -> None:
        """Emit bridge status for pi compaction events."""
        etype = event.get("type")
        if etype in {"auto_compaction_start", "compaction_start"}:
            await self._emit_output(
                session_id,
                "combined",
                "[compacting context...]\n",
                kind="step",
                is_final=False,
                bridge_segments=_bridge_segment("status", "compacting context..."),
            )
            return

        result = event.get("result")
        if result:
            self._context_warning_sent.discard(session_id)
            tokens_before = result.get("tokensBefore", 0)
            await self._emit_output(
                session_id,
                "combined",
                f"[compaction done, was {tokens_before} tokens]\n",
                kind="step",
                is_final=False,
                bridge_segments=_bridge_segment(
                    "status",
                    f"compaction done, was {tokens_before} tokens",
                ),
            )
        elif event.get("errorMessage"):
            message = str(event.get("errorMessage"))
            await self._emit_output(
                session_id,
                "combined",
                f"[compaction failed: {message}]\n",
                kind="step",
                is_final=False,
                bridge_segments=_bridge_segment(
                    "status",
                    f"compaction failed: {message}",
                ),
            )
            if event.get("willRetry") is False:
                await self._events.on_error(
                    session_id,
                    "PI_COMPACTION_FAILED",
                    f"Context recovery failed: {message}",
                )

    async def _handle_retry_event(self, session_id: str, event: dict) -> None:
        """Emit retry status or errors."""
        etype = event.get("type")
        if etype == "auto_retry_start":
            attempt = event.get("attempt", 0)
            max_attempts = event.get("maxAttempts", 0)
            delay_ms = event.get("delayMs", 0)
            await self._emit_output(
                session_id,
                "combined",
                f"[retry {attempt}/{max_attempts}, waiting {delay_ms}ms...]\n",
                kind="step",
                is_final=False,
                bridge_segments=_bridge_segment(
                    "status",
                    f"retry {attempt}/{max_attempts}, waiting {delay_ms}ms",
                ),
            )
            return

        success = event.get("success", False)
        if success:
            return

        self._is_streaming.pop(session_id, False)
        self._streamed_text.pop(session_id, False)
        error = event.get("finalError", "Unknown")
        message = f"Retry failed: {error}"
        session = store.get_session(session_id)
        if session and session.state == SessionState.AWAITING_INPUT:
            await self._emit_output(
                session_id,
                "combined",
                f"[notify] {message}\n",
                kind="step",
                is_final=False,
                bridge_segments=_bridge_segment("status", message),
            )
            return
        await self._events.on_error(session_id, "PI_RETRY_FAILED", message)

    async def _advance_external_sync_cursor(self, session_id: str) -> None:
        """Advance the history cursor for a live attached pi session."""

        session = store.get_session(session_id)
        if not session:
            return
        adapter = str(getattr(session, "adapter", "") or "").lower()
        runner_type = str(getattr(session, "runner_type", "") or "").lower()
        external_type = str(getattr(session, "external_agent_type", "") or "").lower()
        if external_type and external_type != "pi":
            return
        if adapter != "pi_rpc" and runner_type != "pi" and external_type != "pi":
            return
        external_id = store.get_runner_session_id(session_id)
        if not external_id:
            return

        try:
            detail = await asyncio.to_thread(
                get_pi_session_detail,
                external_id,
                500,
            )
        except Exception:
            logger.exception(
                "Failed to advance pi external sync cursor",
                session_id=session_id,
                external_id=external_id,
            )
            return
        if not detail:
            return

        message_count = len(detail.messages)
        if message_count < store.get_synced_message_count(session_id):
            return
        turn_count = sum(1 for message in detail.messages if message.role == "user")
        store.set_synced_message_count(session_id, message_count, turn_count)

    async def _handle_extension_ui_request(self, session_id: str, event: dict) -> None:
        """Forward pi extension UI requests to Tether bridge prompts."""

        method = str(event.get("method") or "")
        if method == "notify":
            msg = event.get("message", "")
            if msg:
                await self._emit_output(
                    session_id,
                    "combined",
                    f"[notify] {msg}\n",
                    kind="step",
                    is_final=False,
                    bridge_segments=_bridge_segment("status", msg),
                )
                self._assistant_marker_needed[session_id] = True
            return
        if method in {"setStatus", "setWidget"}:
            return

        request_id = str(event.get("id") or f"extui_{uuid.uuid4().hex[:12]}")
        tether_request_id = f"pi_extui:{method}:{request_id}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        store.add_pending_permission(
            session_id,
            tether_request_id,
            f"pi extension UI: {method}",
            event,
            future,
        )
        asyncio.create_task(
            self._wait_for_extension_ui_response(
                session_id,
                tether_request_id,
                request_id,
                method,
                event,
                future,
            )
        )

        title = str(event.get("title") or "Input needed")
        if method == "select":
            options = [str(item) for item in event.get("options", []) if str(item)]
            await self._events.on_permission_request(
                session_id,
                request_id=tether_request_id,
                tool_name="AskUserQuestion",
                tool_input={
                    "questions": [
                        {
                            "header": title,
                            "question": title,
                            "options": [{"label": item} for item in options],
                        }
                    ]
                },
            )
        elif method == "confirm":
            await self._events.on_permission_request(
                session_id,
                request_id=tether_request_id,
                tool_name="AskUserQuestion",
                tool_input={
                    "questions": [
                        {
                            "header": title,
                            "question": str(event.get("message") or title),
                            "options": [{"label": "Yes"}, {"label": "No"}],
                        }
                    ]
                },
            )
        elif method in {"input", "editor"}:
            prompt = str(
                event.get("placeholder")
                or event.get("prefill")
                or "Reply with the answer."
            )
            await self._events.on_permission_request(
                session_id,
                request_id=tether_request_id,
                tool_name="Input needed",
                tool_input={"prompt": title, "details": prompt},
            )
        else:
            store.resolve_pending_permission(
                session_id,
                tether_request_id,
                {
                    "behavior": "deny",
                    "message": f"Unsupported extension UI method: {method}",
                },
            )

    async def _wait_for_extension_ui_response(
        self,
        session_id: str,
        tether_request_id: str,
        pi_request_id: str,
        method: str,
        event: dict,
        future: asyncio.Future,
    ) -> None:
        proc = self._processes.get(session_id)
        if not proc or proc.returncode is not None:
            return
        try:
            result = await asyncio.wait_for(future, timeout=PERMISSION_TIMEOUT)
        except asyncio.TimeoutError:
            result = {"behavior": "deny", "message": "Timed out"}
        allowed = result.get("behavior") == "allow"
        value = ""
        updated = result.get("updated_input")
        if isinstance(updated, dict):
            value = str(updated.get("value") or "")
        if not value:
            value = str(result.get("message") or "")

        if not allowed:
            response = {
                "type": "extension_ui_response",
                "id": pi_request_id,
                "cancelled": True,
            }
        elif method == "confirm":
            response = {
                "type": "extension_ui_response",
                "id": pi_request_id,
                "confirmed": value.strip().casefold() not in {"no", "false", "cancel"},
            }
        else:
            response = {
                "type": "extension_ui_response",
                "id": pi_request_id,
                "value": value,
            }

        await self._write_cmd_async(proc, response)
        await self._events.on_permission_resolved(
            session_id,
            request_id=tether_request_id,
            resolved_by="user" if allowed else "system",
            allowed=allowed,
            message=value or str(result.get("message") or ""),
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
            elif command == "compact":
                await self._emit_output(
                    session_id,
                    "combined",
                    f"[compaction failed: {error}]\n",
                    kind="step",
                    is_final=False,
                    bridge_segments=_bridge_segment(
                        "status",
                        f"compaction failed: {error}",
                    ),
                )

        if command == "get_state":
            data = event.get("data", {})
            model_info = data.get("model")
            if isinstance(model_info, dict):
                model_name = model_info.get("name", "unknown")
                model_id = model_info.get("id", "unknown")
                provider = model_info.get("provider", "unknown")
                # ASVS 2.2.1: reject malformed model limits from the RPC boundary.
                try:
                    context_window = int(model_info.get("contextWindow") or 0)
                except (TypeError, ValueError, OverflowError):
                    context_window = 0
                if context_window > 0:
                    self._context_windows[session_id] = context_window
                await self._events.on_header(
                    session_id,
                    title=f"Pi — {model_name}",
                    model=model_id,
                    provider=provider,
                )

            self._auto_compaction_enabled[session_id] = bool(
                data.get("autoCompactionEnabled", False)
            )
            session_file = data.get("sessionFile")
            if session_file:
                self._session_files[session_id] = session_file
                # Extract session UUID from the file path
                uuid_match = re.search(
                    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                    session_file,
                )
                if uuid_match:
                    pi_session_id = uuid_match.group(1)
                    self._session_files[session_id] = session_file
                    store.set_runner_session_id(session_id, pi_session_id)
