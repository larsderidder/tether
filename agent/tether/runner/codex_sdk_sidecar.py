"""Runner adapter that delegates execution to the local sidecar service."""

from __future__ import annotations

import asyncio
import http.client
import json
import re
import socket
import time
import urllib.parse
from typing import Any, Coroutine

import structlog

from tether.runner.base import RunnerEvents
from tether.runner.base import RunnerUnavailableError
from tether.settings import settings
from tether.store import store

logger = structlog.get_logger(__name__)


HEARTBEAT_INTERVAL = 5.0


class SidecarRunner:
    """Runner that prefers the Codex sidecar and falls back to the Codex CLI."""

    runner_type: str = "codex"

    def __init__(self, events: RunnerEvents) -> None:
        self._events = events
        self._base_url = settings.codex_sidecar_url()
        self._token = settings.codex_sidecar_token()
        self._streams: dict[str, asyncio.Task] = {}
        self._cli_processes: dict[str, asyncio.subprocess.Process] = {}
        self._cli_readers: dict[str, asyncio.Task] = {}
        self._cli_heartbeats: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self, session_id: str, prompt: str, approval_choice: int) -> None:
        """Start a sidecar-backed session and subscribe to its SSE stream.

        Args:
            session_id: Internal session identifier.
            prompt: Initial prompt to send to the sidecar.
            approval_choice: Approval policy hint from the UI.
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        store.clear_stop_requested(session_id)
        workdir = store.get_workdir(session_id)

        # Check if this is a resumed/attached session
        thread_id = store.get_runner_session_id(session_id)
        if thread_id:
            logger.info(
                "Starting with attached thread",
                session_id=session_id,
                thread_id=thread_id,
            )

        payload: dict[str, str | int | None] = {
            "session_id": session_id,
            "prompt": prompt,
            "approval_choice": approval_choice,
            "workdir": workdir,
        }

        # Only include thread_id if resuming
        if thread_id:
            payload["thread_id"] = thread_id

        try:
            await self._post_json("/sessions/start", payload)
        except (RunnerUnavailableError, RuntimeError) as exc:
            if not self._should_fallback_to_cli(exc):
                raise
            logger.warning(
                "Codex sidecar unavailable; falling back to Codex CLI",
                session_id=session_id,
                error=str(exc),
            )
            await self._spawn_cli_turn(session_id, prompt, approval_choice)
            return
        self._ensure_stream(session_id)

    async def send_input(self, session_id: str, text: str) -> None:
        """Send follow-up input to the sidecar.

        Args:
            session_id: Internal session identifier.
            text: Follow-up input to send.
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        proc = self._cli_processes.get(session_id)
        if proc and proc.returncode is None:
            store.add_pending_input(session_id, text)
            return
        payload = {"session_id": session_id, "text": text}
        try:
            await self._post_json("/sessions/input", payload)
        except (RunnerUnavailableError, RuntimeError) as exc:
            if not self._should_fallback_to_cli(exc):
                raise
            logger.warning(
                "Codex sidecar unavailable during input; using Codex CLI",
                session_id=session_id,
                error=str(exc),
            )
            await self._spawn_cli_turn(
                session_id, text, self._approval_choice(session_id)
            )
            return
        # Ensure the SSE stream is active in case the sidecar restarted.
        self._ensure_stream(session_id)

    async def stop(self, session_id: str) -> int | None:
        """Interrupt the sidecar session.

        Args:
            session_id: Internal session identifier.
        """
        store.request_stop(session_id)
        proc = self._cli_processes.get(session_id)
        if proc and proc.returncode is None:
            await self._stop_cli_process(session_id, proc)
            return 0
        payload = {"session_id": session_id}
        await self._post_json("/sessions/interrupt", payload)
        task = self._streams.pop(session_id, None)
        if task:
            task.cancel()
        return None

    def update_permission_mode(self, session_id: str, approval_choice: int) -> None:
        """Codex sidecar owns the approval policy; nothing to do here."""
        pass

    async def _spawn_cli_turn(
        self, session_id: str, prompt: str, approval_choice: int
    ) -> None:
        """Run a single Codex turn directly via the local CLI."""
        thread_id = store.get_runner_session_id(session_id)
        workdir = store.get_workdir(session_id)
        cmd = self._build_cli_command(
            prompt=prompt,
            approval_choice=approval_choice,
            workdir=workdir,
            thread_id=thread_id,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RunnerUnavailableError(
                "Codex CLI is not installed or not on PATH. "
                "Install `codex`, or set TETHER_CODEX_SIDECAR_CODEX_BIN."
            ) from exc

        self._cli_processes[session_id] = proc
        store.set_process(session_id, proc)
        await self._write_prompt(proc, prompt)

        start_time = time.monotonic()
        self._cli_heartbeats[session_id] = asyncio.create_task(
            self._emit_cli_heartbeats(session_id, proc, start_time)
        )
        self._cli_readers[session_id] = asyncio.create_task(
            self._read_cli_events(session_id, proc, approval_choice, start_time)
        )

    async def _write_prompt(
        self, proc: asyncio.subprocess.Process, prompt: str
    ) -> None:
        if proc.stdin is None:
            return
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        if proc.stdin.can_write_eof():
            proc.stdin.write_eof()

    async def _stop_cli_process(
        self, session_id: str, proc: asyncio.subprocess.Process
    ) -> None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

        reader = self._cli_readers.pop(session_id, None)
        if reader and not reader.done():
            try:
                await asyncio.wait_for(reader, timeout=2.0)
            except asyncio.TimeoutError:
                reader.cancel()
                try:
                    await reader
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass

    async def _emit_cli_heartbeats(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
        start_time: float,
    ) -> None:
        try:
            while proc.returncode is None:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if proc.returncode is not None:
                    return
                await self._events.on_heartbeat(
                    session_id, time.monotonic() - start_time, done=False
                )
        except asyncio.CancelledError:
            return

    async def _read_cli_events(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
        approval_choice: int,
        start_time: float,
    ) -> None:
        try:
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(
                        "Malformed JSON from codex CLI",
                        session_id=session_id,
                        raw=raw[:200],
                    )
                    continue
                await self._handle_cli_event(session_id, event)
        except asyncio.CancelledError:
            logger.info("Codex CLI reader cancelled", session_id=session_id)
            raise
        except Exception:
            logger.exception("Codex CLI reader failed", session_id=session_id)
            await self._events.on_error(
                session_id,
                "CODEX_CLI_READER_ERROR",
                "Codex CLI reader crashed",
            )
        finally:
            heartbeat = self._cli_heartbeats.pop(session_id, None)
            if heartbeat and not heartbeat.done():
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass

            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

            if proc.stderr:
                try:
                    stderr_data = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
                except (asyncio.TimeoutError, Exception):
                    stderr_data = b""
                if stderr_data:
                    for line in stderr_data.decode(errors="replace").splitlines():
                        if line.strip():
                            logger.debug(
                                "Codex CLI stderr", session_id=session_id, line=line
                            )

            self._cli_processes.pop(session_id, None)
            self._cli_readers.pop(session_id, None)
            store.clear_process(session_id)

            elapsed = time.monotonic() - start_time
            await self._events.on_heartbeat(session_id, elapsed, done=True)

            next_input = store.pop_next_pending_input(session_id)
            if (
                next_input
                and proc.returncode in (0, None)
                and not store.is_stop_requested(session_id)
            ):
                await self._spawn_cli_turn(
                    session_id, next_input, self._approval_choice(session_id)
                )
                return

            if store.is_stop_requested(session_id):
                await self._events.on_exit(session_id, proc.returncode)
            elif proc.returncode in (0, None):
                await self._events.on_awaiting_input(session_id)
            else:
                await self._events.on_exit(session_id, proc.returncode)

    async def _handle_cli_event(self, session_id: str, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "thread.started":
            await self._events.on_header(
                session_id,
                title="Codex CLI",
                model=settings.codex_sidecar_model() or None,
                provider="OpenAI (Codex CLI)",
                sandbox=self._cli_sandbox_mode(),
                approval=self._cli_approval_policy(self._approval_choice(session_id)),
                thread_id=event.get("thread_id"),
            )
            return

        if event_type == "item.started":
            item = event.get("item", {})
            if item.get("type") == "command_execution":
                text = self._format_cli_item(item)
                if text:
                    await self._events.on_output(
                        session_id,
                        "combined",
                        f"{text}\n",
                        kind="step",
                        is_final=False,
                    )
            return

        if event_type == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type")
            if item_type == "agent_message":
                text = item.get("text", "")
                await self._events.on_output(
                    session_id,
                    "combined",
                    text,
                    kind="final",
                    is_final=True,
                )
                return

            if item_type == "error":
                await self._events.on_error(
                    session_id,
                    "INTERNAL_ERROR",
                    item.get("message", "Unknown error"),
                )
                return

            text = self._format_cli_item(item)
            if text:
                await self._events.on_output(
                    session_id,
                    "combined",
                    f"{text}\n",
                    kind="step",
                    is_final=False,
                )
            return

        if event_type == "turn.completed":
            usage = event.get("usage", {})
            input_tokens = int(usage.get("input_tokens", 0) or 0)
            cached_input_tokens = int(usage.get("cached_input_tokens", 0) or 0)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            total = input_tokens + cached_input_tokens + output_tokens
            await self._events.on_metadata(
                session_id, "input_tokens", input_tokens, str(input_tokens)
            )
            await self._events.on_metadata(
                session_id,
                "cached_input_tokens",
                cached_input_tokens,
                str(cached_input_tokens),
            )
            await self._events.on_metadata(
                session_id, "output_tokens", output_tokens, str(output_tokens)
            )
            await self._events.on_metadata(
                session_id, "tokens_used", total, str(total)
            )
            return

        if event_type == "turn.failed":
            error = event.get("error", {})
            message = error.get("message") or error.get("code") or "Turn failed"
            await self._events.on_error(session_id, "INTERNAL_ERROR", message)
            return

        if event_type == "error":
            await self._events.on_error(
                session_id,
                "INTERNAL_ERROR",
                event.get("message", "Unknown error"),
            )

    def _should_fallback_to_cli(self, exc: Exception) -> bool:
        if isinstance(exc, RunnerUnavailableError):
            return True
        if not isinstance(exc, RuntimeError):
            return False
        match = re.search(r"Sidecar request failed:\s*(\d{3})", str(exc))
        if not match:
            return False
        return int(match.group(1)) in {404, 405, 501}

    def _build_cli_command(
        self,
        *,
        prompt: str,
        approval_choice: int,
        workdir: str | None,
        thread_id: str | None,
    ) -> list[str]:
        del prompt
        cmd = [settings.codex_sidecar_codex_bin() or "codex"]
        if workdir:
            cmd.extend(["-C", workdir])

        model = settings.codex_sidecar_model()
        if model:
            cmd.extend(["-m", model])

        sandbox_mode = self._cli_sandbox_mode()
        approval_policy = self._cli_approval_policy(approval_choice)
        if approval_policy == "never" and sandbox_mode in (None, "danger-full-access"):
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            if sandbox_mode:
                cmd.extend(["--sandbox", sandbox_mode])
            if approval_policy:
                cmd.extend(["-a", approval_policy])

        cmd.append("exec")
        if thread_id:
            cmd.extend(["resume", thread_id])

        cmd.extend(["--json", "--skip-git-repo-check", "-"])
        return cmd

    def _approval_choice(self, session_id: str) -> int:
        session = store.get_session(session_id)
        approval_mode = session.approval_mode if session else None
        return approval_mode if approval_mode is not None else 2

    def _cli_approval_policy(self, approval_choice: int) -> str:
        override = settings.codex_sidecar_approval_policy()
        if override:
            return override
        if approval_choice == 2:
            return "never"
        if approval_choice == 1:
            return "on-failure"
        return "on-request"

    def _cli_sandbox_mode(self) -> str | None:
        raw = settings.codex_sidecar_sandbox_mode().strip().lower()
        if not raw:
            return None
        mapping = {
            "workspace-write": "workspace-write",
            "workspace-read-only": "read-only",
            "read-only": "read-only",
            "none": "danger-full-access",
            "danger-full-access": "danger-full-access",
        }
        return mapping.get(raw, raw)

    def _format_cli_item(self, item: dict[str, Any]) -> str:
        item_type = item.get("type")
        if item_type == "reasoning":
            return str(item.get("text", "")).strip()
        if item_type == "command_execution":
            command = str(item.get("command", "")).strip()
            if not command:
                return ""
            exit_code = item.get("exit_code")
            if exit_code is None:
                return f"Command: {command}"
            return f"Command: {command} (exit {exit_code})"
        if item_type == "file_change":
            changes = item.get("changes") or []
            return f"File change: {len(changes)} file(s)"
        if item_type == "mcp_tool_call":
            server = str(item.get("server", "")).strip()
            tool = str(item.get("tool", "")).strip()
            if server and tool:
                return f"MCP: {server}.{tool}"
            return ""
        if item_type == "web_search":
            query = str(item.get("query", "")).strip()
            return f"Web search: {query}" if query else ""
        if item_type == "todo_list":
            items = item.get("items") or []
            remaining = sum(1 for todo in items if not todo.get("completed"))
            return f"Todo list: {remaining} remaining"
        if item_type == "error":
            message = str(item.get("message", "")).strip()
            return f"Error: {message}" if message else ""
        return ""

    def _ensure_stream(self, session_id: str) -> None:
        """Start SSE consumption for a session if not already running."""
        existing = self._streams.get(session_id)
        if existing and not existing.done():
            return
        if existing and existing.done():
            self._streams.pop(session_id, None)
        self._streams[session_id] = asyncio.create_task(self._consume_stream(session_id))

    async def _consume_stream(self, session_id: str) -> None:
        """Consume sidecar SSE events in a background thread.

        Args:
            session_id: Internal session identifier.
        """
        backoff_s = 0.5
        max_backoff_s = 5.0
        while True:
            if store.is_stop_requested(session_id):
                return
            try:
                should_retry = await asyncio.to_thread(self._stream_worker, session_id)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("Sidecar stream failed", session_id=session_id)
                await self._events.on_error(
                    session_id,
                    "STREAM_ERROR",
                    f"Sidecar stream failed: {exc}",
                )
                should_retry = True
            if not should_retry:
                return
            await asyncio.sleep(backoff_s)
            backoff_s = min(max_backoff_s, backoff_s * 2)

    def _stream_worker(self, session_id: str) -> bool:
        """Blocking SSE reader that forwards events to the asyncio loop.

        Args:
            session_id: Internal session identifier.
        """
        url = urllib.parse.urlparse(self._base_url)
        conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=30)
        path = f"/events/{session_id}"
        headers = {}
        if self._token:
            headers["X-Sidecar-Token"] = self._token
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
        except (socket.timeout, OSError) as exc:
            logger.error("Sidecar connection failed", session_id=session_id, error=str(exc))
            self._dispatch(
                self._events.on_error(
                    session_id, "CONNECTION_ERROR", f"Sidecar connection failed: {exc}"
                )
            )
            conn.close()
            return True
        if resp.status != 200:
            data = resp.read().decode("utf-8", errors="replace")
            logger.error("Sidecar SSE failed", session_id=session_id, status=resp.status, body=data)
            self._dispatch(self._events.on_error(session_id, "SIDECAR_ERROR", f"Sidecar returned {resp.status}"))
            conn.close()
            return resp.status >= 500
        # Set per-read timeout on the socket (60s to allow for heartbeat intervals)
        read_timeout = 60.0
        if conn.sock:
            conn.sock.settimeout(read_timeout)
        try:
            while True:
                try:
                    line = resp.fp.readline().decode("utf-8", errors="replace")
                except socket.timeout:
                    logger.warning("Sidecar read timeout", session_id=session_id, timeout_s=read_timeout)
                    self._dispatch(self._events.on_error(session_id, "READ_TIMEOUT", "Sidecar stream timed out"))
                    return True
                if not line:
                    # Empty line means connection closed
                    logger.info("Sidecar stream closed", session_id=session_id)
                    return True
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: ") :].strip()
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError as exc:
                    logger.warning("Failed to parse SSE event", session_id=session_id, payload=payload[:200], error=str(exc))
                    continue
                logger.debug("Received sidecar event", session_id=session_id, event_type=event.get("type"))
                self._handle_event(session_id, event)
        finally:
            conn.close()
        return True

    def _handle_event(self, session_id: str, event: dict) -> None:
        """Route sidecar events to the event callbacks.

        Args:
            session_id: Internal session identifier.
            event: Parsed event payload from the sidecar stream.
        """
        event_type = event.get("type")
        data = event.get("data", {})
        if event_type == "header":
            # Structured header from sidecar
            title = data.get("title", "Codex")
            model = data.get("model")
            provider = data.get("provider")
            sandbox = data.get("sandbox")
            approval = data.get("approval")
            thread_id = data.get("thread_id")
            self._dispatch(
                self._events.on_header(
                    session_id,
                    title=title,
                    model=model,
                    provider=provider,
                    sandbox=sandbox,
                    approval=approval,
                    thread_id=thread_id,
                )
            )
            return
        if event_type == "output":
            text = data.get("text", "")
            kind = data.get("kind")
            if kind:
                is_final = data.get("final")
                self._dispatch(
                    self._events.on_output(
                        session_id, "combined", text, kind=kind, is_final=is_final
                    )
                )
            else:
                logger.warning("Sidecar output missing kind field", session_id=session_id)
        elif event_type == "metadata":
            key = data.get("key")
            value = data.get("value")
            raw = data.get("raw", "")
            if key:
                self._dispatch(self._events.on_metadata(session_id, key, value, raw))
        elif event_type == "heartbeat":
            elapsed_s = float(data.get("elapsed_s", 0.0))
            done = bool(data.get("done", False))
            self._dispatch(self._events.on_heartbeat(session_id, elapsed_s, done))
        elif event_type == "error":
            code = data.get("code", "INTERNAL_ERROR")
            message = data.get("message", "Unknown error")
            self._dispatch(self._events.on_error(session_id, code, message))
        elif event_type == "exit":
            exit_code = data.get("exit_code")
            # If stop was explicitly requested or exit code is non-zero, it's a real exit
            # Otherwise, the agent finished a turn and is waiting for input
            if store.is_stop_requested(session_id) or exit_code not in (0, None):
                self._dispatch(self._events.on_exit(session_id, exit_code))
            else:
                self._dispatch(self._events.on_awaiting_input(session_id))
        else:
            logger.debug("Unknown sidecar event type", session_id=session_id, event_type=event_type)

    def _dispatch(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Schedule an event callback on the agent's asyncio loop.

        Args:
            coro: Coroutine to schedule on the main loop.
        """
        if not self._loop:
            logger.warning("Cannot dispatch event: event loop not set")
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _post_json(self, path: str, payload: dict) -> None:
        """POST JSON to the sidecar and raise on non-2xx responses.

        Args:
            path: Sidecar API path, e.g. "/sessions/start".
            payload: JSON body to send.
        """
        url = urllib.parse.urlparse(self._base_url)
        if not url.hostname:
            raise RunnerUnavailableError(f"Invalid sidecar URL: {self._base_url}")
        conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=10)
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Sidecar-Token"] = self._token
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read().decode("utf-8", errors="replace")
        except (ConnectionRefusedError, socket.timeout, OSError) as exc:
            raise RunnerUnavailableError(
                "Agent backend is not reachable "
                f"at {self._base_url} ({exc}). "
                "If you're using Codex sidecar, start it with: "
                "make start-codex."
            ) from exc
        finally:
            conn.close()
        if resp.status < 200 or resp.status >= 300:
            raise RuntimeError(f"Sidecar request failed: {resp.status} {data}")
