"""Runner adapter that shells out to the Codex CLI and normalizes output.

.. deprecated::
    This runner is deprecated in favor of the Codex SDK sidecar approach.
    The sidecar provides structured events directly, avoiding the brittle
    stdout/stderr parsing required by this runner.
    Use TETHER_AGENT_ADAPTER=sidecar instead.
"""

from __future__ import annotations

import asyncio
import os
import time
import re
from collections import deque
import structlog

from tether.models import SessionState
from tether.runner.base import RunnerEvents
from tether.settings import settings
from tether.store import store

logger = structlog.get_logger("tether.runner.codex_cli")
SESSION_ID_RE = re.compile(r"(?:session id|session_id)[:=]\s*(\S+)", re.IGNORECASE)


class CodexCliRunner:
    """Runner that shells out to the Codex CLI and parses its stdout/stderr."""

    runner_type: str = "codex"

    def __init__(self, events: RunnerEvents) -> None:
        self._events = events
        self._recent_output: dict[str, deque[str]] = {}
        self._skip_next_tokens_line: dict[str, bool] = {}
        self._recent_prompts: dict[str, deque[str]] = {}
        self._last_output: dict[str, str] = {}
        self._in_thinking: dict[str, bool] = {}
        self._output_locks: dict[str, asyncio.Lock] = {}
        self._capturing_header: dict[str, bool] = {}
        self._saw_separator: dict[str, bool] = {}
        self._header_lines: dict[str, list[str]] = {}
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}
        self._heartbeat_start: dict[str, float] = {}

    async def start(self, session_id: str, prompt: str, approval_choice: int) -> None:
        """Start a Codex CLI session and stream output via events.

        Args:
            session_id: Internal session identifier.
            prompt: Initial prompt to send to Codex.
            approval_choice: Approval policy hint from the UI.
        """
        store.clear_stop_requested(session_id)
        self._clear_session_state(session_id)
        if prompt:
            self._remember_prompt(session_id, prompt)
        args = ["exec", "--skip-git-repo-check"]
        if approval_choice == 2:
            logger.warning(
                "approval_choice ignored for codex exec; interactive approvals not supported",
                session_id=session_id,
            )
        if prompt:
            args.append(prompt)
        asyncio.create_task(self._run_exec(session_id, args))

    async def send_input(self, session_id: str, text: str) -> None:
        """Queue or resume a Codex session with follow-up input.

        Args:
            session_id: Internal session identifier.
            text: Follow-up input to send.
        """
        if text:
            self._remember_prompt(session_id, text)
        runner_session_id = store.get_runner_session_id(session_id)
        if store.get_process(session_id) or not runner_session_id:
            store.add_pending_input(session_id, text)
            logger.info(
                "Queued input",
                session_id=session_id,
                has_process=bool(store.get_process(session_id)),
                has_runner_session_id=bool(runner_session_id),
            )
            return
        args = ["exec", "--skip-git-repo-check", "resume", runner_session_id, text]
        logger.info("Starting resume exec", session_id=session_id, runner_session_id=runner_session_id)
        asyncio.create_task(self._run_exec(session_id, args))

    async def stop(self, session_id: str) -> int | None:
        """Terminate a running process and clear Codex-specific state.

        Args:
            session_id: Internal session identifier.
        """
        store.request_stop(session_id)
        proc = store.get_process(session_id)
        exit_code = None
        if proc and proc.returncode is None:
            logger.info("Stopping process", session_id=session_id, pid=proc.pid)
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                logger.warning("Process did not terminate, killing", session_id=session_id, pid=proc.pid)
                proc.kill()
                await proc.wait()
        if proc:
            exit_code = proc.returncode
        store.clear_process(session_id)
        store.clear_last_output(session_id)
        store.clear_master_fd(session_id)
        store.clear_stdin(session_id)
        store.clear_prompt_sent(session_id)
        store.clear_pending_inputs(session_id)
        store.clear_runner_session_id(session_id)
        store.clear_workdir(session_id, force=False)
        self._clear_session_state(session_id)
        return exit_code

    async def _read_stream(self, session_id: str, stream: asyncio.StreamReader) -> None:
        """Read stdout lines, capture session id, and forward output.

        Args:
            session_id: Internal session identifier.
            stream: Async byte stream for process stdout.
        """
        session = store.get_session(session_id)
        if not session:
            return
        async for line in stream:
            session = store.get_session(session_id)
            if not session or session.state != SessionState.RUNNING:
                return
            session.last_activity_at = store._now()
            store.update_session(session)
            text = line.decode("utf-8", errors="replace")
            await self._handle_output(session_id, text)

    async def _log_stderr(self, session_id: str, stream: asyncio.StreamReader) -> None:
        """Read stderr lines, capture header blocks, and forward output.

        Args:
            session_id: Internal session identifier.
            stream: Async byte stream for process stderr.
        """
        async for line in stream:
            session = store.get_session(session_id)
            if not session or session.state != SessionState.RUNNING:
                return
            text = line.decode("utf-8", errors="replace")
            logger.info("codex stderr", session_id=session_id, text=text.rstrip())
            await self._handle_output(session_id, text)

    async def _run_exec(self, session_id: str, args: list[str]) -> None:
        """Spawn the Codex CLI process and wire up stdout/stderr tasks.

        Args:
            session_id: Internal session identifier.
            args: Arguments to pass to the Codex CLI.
        """
        session = store.get_session(session_id)
        if not session:
            return
        try:
            cmd = settings.codex_bin()
            if not cmd:
                raise FileNotFoundError("TETHER_AGENT_CODEX_BIN is not set")
            if not os.path.isfile(cmd):
                raise FileNotFoundError(f"CODEX_BIN not found: {cmd}")
            if not os.access(cmd, os.X_OK):
                raise PermissionError(f"CODEX_BIN not executable: {cmd}")
            workdir = store.get_workdir(session_id) or store.create_workdir(session_id)
            logger.info("Starting codex exec", session_id=session_id, cmd=cmd, args=args)
            proc = await asyncio.create_subprocess_exec(
                cmd,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
            )
            store.set_process(session_id, proc)
            logger.info("Process started", session_id=session_id, pid=proc.pid)
            self._start_heartbeat(session_id)
            stdout = proc.stdout or asyncio.StreamReader()
            stderr = proc.stderr or asyncio.StreamReader()
            reader_tasks = [
                asyncio.create_task(self._read_stream(session_id, stdout)),
                asyncio.create_task(self._log_stderr(session_id, stderr)),
            ]
            timeout_s = settings.turn_timeout_seconds()
            if timeout_s > 0:
                try:
                    exit_code = await asyncio.wait_for(proc.wait(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    logger.warning("Turn timeout reached; terminating process", session_id=session_id)
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                    store.clear_process(session_id)
                    store.clear_last_output(session_id)
                    await self._stop_heartbeat(session_id, done=True)
                    await self._events.on_error(session_id, "TIMEOUT", "Runner turn timed out")
                    return
            else:
                exit_code = await proc.wait()
            logger.info("Process exited", session_id=session_id, exit_code=exit_code)
            for task in reader_tasks:
                task.cancel()
            store.clear_process(session_id)
            store.clear_last_output(session_id)
        except Exception as exc:
            logger.exception("Process runner failed", session_id=session_id)
            store.clear_process(session_id)
            store.clear_last_output(session_id)
            await self._stop_heartbeat(session_id, done=True)
            await self._events.on_error(session_id, "INTERNAL_ERROR", str(exc))
            return

        await self._stop_heartbeat(session_id, done=True)
        # If stop was explicitly requested or exit code is non-zero, it's a real exit
        # Otherwise, the agent finished a turn and is waiting for input
        if store.is_stop_requested(session_id) or exit_code not in (0, None):
            await self._events.on_exit(session_id, exit_code)
        else:
            await self._events.on_awaiting_input(session_id)
            await self._maybe_run_pending(session_id)

    async def _maybe_run_pending(self, session_id: str) -> None:
        """Run the next queued input if the runner session id is available.

        Args:
            session_id: Internal session identifier.
        """
        session = store.get_session(session_id)
        if not session or session.state != SessionState.RUNNING:
            return
        if store.get_process(session_id):
            return
        runner_session_id = store.get_runner_session_id(session_id)
        if not runner_session_id:
            logger.info("No runner session id yet", session_id=session_id)
            return
        next_text = store.pop_next_pending_input(session_id)
        if not next_text:
            return
        args = ["exec", "--skip-git-repo-check", "resume", runner_session_id, next_text]
        logger.info("Running pending input", session_id=session_id, runner_session_id=runner_session_id)
        asyncio.create_task(self._run_exec(session_id, args))

    async def _handle_output(self, session_id: str, text: str) -> None:
        """Serialize output handling per session to avoid interleaving.

        Args:
            session_id: Internal session identifier.
            text: Raw output chunk from the process.
        """
        lock = self._output_locks.get(session_id)
        if not lock:
            lock = asyncio.Lock()
            self._output_locks[session_id] = lock
        async with lock:
            await self._handle_output_locked(session_id, text)

    async def _handle_output_locked(self, session_id: str, text: str) -> None:
        """Parse raw output lines and emit normalized output events.

        Args:
            session_id: Internal session identifier.
            text: Raw output chunk from the process.
        """
        for line in text.splitlines(keepends=True):
            if not line:
                continue
            raw = line.rstrip("\n")
            if await self._maybe_capture_header(session_id, raw):
                continue
            # "tokens used" is emitted as either inline or the next numeric line.
            if self._skip_next_tokens_line.pop(session_id, False):
                if re.fullmatch(r"[0-9][0-9,]*", raw.strip().lower()):
                    value = int(raw.strip().replace(",", ""))
                    await self._events.on_metadata(
                        session_id,
                        "tokens_used",
                        value,
                        raw.strip(),
                    )
                    continue
            if raw.strip().lower().startswith("tokens used"):
                match = re.search(r"([0-9][0-9,]*)", raw)
                if match:
                    value = int(match.group(1).replace(",", ""))
                    await self._events.on_metadata(
                        session_id,
                        "tokens_used",
                        value,
                        match.group(1),
                    )
                    continue
            match = SESSION_ID_RE.search(raw)
            if match and not store.get_runner_session_id(session_id):
                store.set_runner_session_id(session_id, match.group(1))
                logger.info(
                    "Captured runner session id",
                    session_id=session_id,
                    runner_session_id=match.group(1),
                )
                continue
            if self._should_skip_line(session_id, raw):
                continue
            normalized = self._normalize_output(raw)
            if not normalized:
                continue
            # Avoid spamming the UI with repeated lines.
            if self._last_output.get(session_id) == normalized:
                continue
            if self._seen_recently(session_id, normalized):
                continue
            self._last_output[session_id] = normalized
            kind = "step" if self._in_thinking.pop(session_id, False) else "final"
            is_final = kind == "final"
            await self._events.on_output(session_id, "combined", line, kind=kind, is_final=is_final)

    async def _maybe_capture_header(self, session_id: str, line: str) -> bool:
        capturing = self._capturing_header.get(session_id, False)
        if "OpenAI Codex" in line and not capturing:
            self._capturing_header[session_id] = True
            self._saw_separator[session_id] = False
            self._header_lines[session_id] = [line]
            return True
        if not capturing:
            return False
        self._header_lines.setdefault(session_id, []).append(line)
        if line.strip() == "--------":
            if self._saw_separator.get(session_id):
                header = "\n".join(self._header_lines.get(session_id, []))
                await self._events.on_output(
                    session_id, "combined", header, kind="header", is_final=None
                )
                self._capturing_header[session_id] = False
                self._saw_separator[session_id] = False
                self._header_lines.pop(session_id, None)
            else:
                self._saw_separator[session_id] = True
        return True

    def _should_skip_line(self, session_id: str, line: str) -> bool:
        """Return True if a line is a prompt echo or Codex metadata.

        Args:
            session_id: Internal session identifier.
            line: Raw output line.
        """
        lower = line.strip().lower()
        if lower in {"user", "codex", "assistant"}:
            return True
        if lower == "thinking":
            self._in_thinking[session_id] = True
            return True
        if lower.startswith("mcp startup:"):
            return True
        if lower.startswith("tokens used"):
            self._skip_next_tokens_line[session_id] = True
            return True
        if self._should_skip_prompt_echo(session_id, self._normalize_output(line)):
            return True
        return False

    def _normalize_output(self, text: str) -> str:
        """Strip ANSI escapes and collapse whitespace for comparisons.

        Args:
            text: Output text to normalize.
        """
        stripped = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
        return " ".join(stripped.strip().split())

    def _seen_recently(self, session_id: str, normalized: str) -> bool:
        """Track de-duplication history for output lines.

        Args:
            session_id: Internal session identifier.
            normalized: Normalized output line.
        """
        history = self._recent_output.get(session_id)
        if history is None:
            history = deque(maxlen=50)
            self._recent_output[session_id] = history
        if normalized in history:
            return True
        history.append(normalized)
        return False

    def _remember_prompt(self, session_id: str, text: str) -> None:
        """Record recent prompts to suppress echoed input.

        Args:
            session_id: Internal session identifier.
            text: Prompt text provided by the user.
        """
        normalized = self._normalize_output(text)
        if not normalized:
            return
        history = self._recent_prompts.get(session_id)
        if history is None:
            history = deque(maxlen=10)
            self._recent_prompts[session_id] = history
        history.append(normalized)

    def _should_skip_prompt_echo(self, session_id: str, line: str) -> bool:
        """Return True if the line matches a recent prompt.

        Args:
            session_id: Internal session identifier.
            line: Normalized output line.
        """
        history = self._recent_prompts.get(session_id)
        if not history:
            return False
        return line in history

    def _clear_session_state(self, session_id: str) -> None:
        """Clear cached parsing state for a session.

        Args:
            session_id: Internal session identifier.
        """
        self._recent_output.pop(session_id, None)
        self._skip_next_tokens_line.pop(session_id, None)
        self._recent_prompts.pop(session_id, None)
        self._last_output.pop(session_id, None)
        self._in_thinking.pop(session_id, None)
        self._output_locks.pop(session_id, None)
        self._capturing_header.pop(session_id, None)
        self._saw_separator.pop(session_id, None)
        self._header_lines.pop(session_id, None)
        task = self._heartbeat_tasks.pop(session_id, None)
        if task:
            task.cancel()
        self._heartbeat_start.pop(session_id, None)

    def _start_heartbeat(self, session_id: str) -> None:
        interval_s = 5.0  # Heartbeat interval in seconds
        self._heartbeat_start[session_id] = time.monotonic()
        task = self._heartbeat_tasks.get(session_id)
        if task:
            task.cancel()
        self._heartbeat_tasks[session_id] = asyncio.create_task(
            self._heartbeat_loop(session_id, interval_s)
        )

    async def _stop_heartbeat(self, session_id: str, done: bool) -> None:
        task = self._heartbeat_tasks.pop(session_id, None)
        if task:
            task.cancel()
        start = self._heartbeat_start.pop(session_id, None)
        if start is None:
            return
        elapsed_s = max(0.0, time.monotonic() - start)
        await self._events.on_heartbeat(session_id, elapsed_s, done)
        await self._events.on_metadata(
            session_id, "duration_ms", int(elapsed_s * 1000), str(int(elapsed_s * 1000))
        )

    async def _heartbeat_loop(self, session_id: str, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            start = self._heartbeat_start.get(session_id)
            if start is None:
                return
            elapsed_s = max(0.0, time.monotonic() - start)
            await self._events.on_heartbeat(session_id, elapsed_s, False)
