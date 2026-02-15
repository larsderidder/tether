"""OpenCode subprocess runner.

Spawns `opencode run --format json --session <id> <prompt>` for each turn.
Similar to claude_subprocess pattern: one process per query turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

import structlog

from tether.runner.base import Runner, RunnerEvents

logger = structlog.get_logger(__name__)


def _find_opencode_bin() -> str | None:
    """Find the opencode binary.

    Checks (in order):
    1. OPENCODE_BIN env var
    2. ~/.opencode/bin/opencode
    3. opencode in PATH
    """
    # Check env var
    env_bin = os.environ.get("OPENCODE_BIN")
    if env_bin:
        path = Path(env_bin).expanduser()
        if path.exists() and path.is_file():
            return str(path)

    # Check standard install location
    home_bin = Path.home() / ".opencode" / "bin" / "opencode"
    if home_bin.exists():
        return str(home_bin)

    # Check PATH
    path_bin = shutil.which("opencode")
    if path_bin:
        return path_bin

    return None


class OpencodeSubprocessRunner(Runner):
    """Runner that executes OpenCode via subprocess, one process per turn."""

    runner_type = "opencode"

    def __init__(self, events: RunnerEvents):
        self.events = events
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._opencode_bin = _find_opencode_bin()
        if not self._opencode_bin:
            raise ValueError(
                "OpenCode binary not found. Install OpenCode or set OPENCODE_BIN environment variable."
            )

    async def start(
        self,
        session_id: str,
        prompt: str,
        approval_choice: str,
    ) -> None:
        """Start a new OpenCode turn.

        Args:
            session_id: Tether session ID (maps to OpenCode session via store)
            prompt: User message
            approval_choice: Permission mode (not used by OpenCode)
        """
        from tether.store import store

        # Get the OpenCode session ID from the store
        opencode_session_id = store.get_runner_session_id(session_id)
        if not opencode_session_id:
            await self.events.on_error(
                session_id,
                "RUNNER_ERROR",
                "No OpenCode session ID found. Cannot resume session.",
            )
            return

        # Get working directory
        workdir = store.get_workdir(session_id)
        if not workdir:
            workdir = os.getcwd()

        logger.info(
            "Starting OpenCode subprocess",
            session_id=session_id,
            opencode_session_id=opencode_session_id,
            workdir=workdir,
        )

        # Build command
        cmd = [
            self._opencode_bin,
            "run",
            "--format",
            "json",
            "--session",
            opencode_session_id,
            prompt,
        ]

        try:
            # Start subprocess
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
            )
            self._processes[session_id] = proc

            # Emit header
            await self.events.on_header(
                session_id,
                title=f"OpenCode: {prompt[:50]}",
                model="opencode",
                provider="opencode",
            )

            # Read stdout line by line
            await self._read_output(session_id, proc)

            # Wait for completion
            exit_code = await proc.wait()

            # Emit final state
            if exit_code == 0:
                await self.events.on_awaiting_input(session_id)
            else:
                stderr = await proc.stderr.read() if proc.stderr else b""
                error_msg = stderr.decode("utf-8", errors="replace").strip()
                await self.events.on_error(
                    session_id,
                    "RUNNER_ERROR",
                    f"OpenCode exited with code {exit_code}: {error_msg}",
                )

            await self.events.on_exit(session_id, exit_code)

        except Exception as exc:
            logger.exception("OpenCode subprocess failed", session_id=session_id)
            await self.events.on_error(
                session_id,
                "RUNNER_ERROR",
                f"Failed to run OpenCode: {exc}",
            )
            await self.events.on_exit(session_id, 1)
        finally:
            self._processes.pop(session_id, None)

    async def _read_output(
        self, session_id: str, proc: asyncio.subprocess.Process
    ) -> None:
        """Read and parse JSON output from OpenCode."""
        if not proc.stdout:
            return

        heartbeat_task = asyncio.create_task(self._heartbeat(session_id))

        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    event = json.loads(line_str)
                except json.JSONDecodeError:
                    # Not JSON, skip
                    continue

                await self._handle_event(session_id, event)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    async def _handle_event(self, session_id: str, event: dict) -> None:
        """Handle a single JSON event from OpenCode."""
        event_type = event.get("type")

        if event_type == "step_start":
            # Agent started processing
            pass

        elif event_type == "text":
            # Text output chunk
            part = event.get("part", {})
            text = part.get("text", "")
            if text:
                await self.events.on_output(
                    session_id,
                    stream="stdout",
                    text=text,
                    kind="text",
                    is_final=False,
                )

        elif event_type == "step_finish":
            # Agent finished, emit final output and metadata
            await self.events.on_output(
                session_id,
                stream="stdout",
                text="",
                kind="text",
                is_final=True,
            )

            # Extract token usage and cost
            part = event.get("part", {})
            tokens = part.get("tokens", {})
            cost = part.get("cost", 0)

            if tokens:
                await self.events.on_metadata(
                    session_id,
                    "tokens_input",
                    tokens.get("input", 0),
                    raw=tokens,
                )
                await self.events.on_metadata(
                    session_id,
                    "tokens_output",
                    tokens.get("output", 0),
                    raw=tokens,
                )
                await self.events.on_metadata(
                    session_id,
                    "tokens_total",
                    tokens.get("total", 0),
                    raw=tokens,
                )

            if cost:
                await self.events.on_metadata(
                    session_id,
                    "cost",
                    cost,
                    raw={"cost": cost},
                )

        # Other event types (if any) are ignored for now

    async def _heartbeat(self, session_id: str) -> None:
        """Send periodic heartbeat signals while the process is running."""
        elapsed = 0.0
        interval = 1.0  # 1 second
        try:
            while True:
                await asyncio.sleep(interval)
                elapsed += interval
                await self.events.on_heartbeat(
                    session_id, elapsed_s=elapsed, done=False
                )
        except asyncio.CancelledError:
            pass

    async def send_input(self, session_id: str, text: str) -> None:
        """Send input to OpenCode.

        OpenCode run mode doesn't support interactive input within a turn.
        Input is only accepted at the start of a new turn via `start()`.
        """
        await self.events.on_error(
            session_id,
            "RUNNER_ERROR",
            "OpenCode does not support mid-turn input. Send input as a new message.",
        )

    async def stop(self, session_id: str) -> int | None:
        """Stop the OpenCode subprocess for a session."""
        proc = self._processes.get(session_id)
        if not proc:
            return None

        logger.info("Stopping OpenCode subprocess", session_id=session_id)

        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("OpenCode did not terminate, killing", session_id=session_id)
            proc.kill()
            await proc.wait()

        exit_code = proc.returncode
        self._processes.pop(session_id, None)
        return exit_code

    def update_permission_mode(self, session_id: str, approval_choice: str) -> None:
        """Update permission mode.

        OpenCode doesn't have a permission system like Claude, so this is a no-op.
        """
        pass
