"""Runner adapter that delegates execution to the local sidecar service."""

from __future__ import annotations

import asyncio
import json
import structlog
import urllib.parse
import http.client
import re
import os
from collections import deque
from typing import Any, Coroutine

from tether.models import SessionState
from tether.store import store

from tether.runner.base import RunnerEvents

logger = structlog.get_logger("tether.runner.sidecar")
SESSION_ID_RE = re.compile(r"(?:session id|session_id)[:=]\s*(\S+)", re.IGNORECASE)


class SidecarRunner:
    """Runner that delegates Codex execution to a local TypeScript sidecar."""

    runner_type: str = "codex"

    def __init__(self, events: RunnerEvents, base_url: str | None = None) -> None:
        self._events = events
        self._base_url = base_url or "http://localhost:8788"
        self._token = os.environ.get("CODEX_SDK_SIDECAR_TOKEN", "") or os.environ.get("SIDECAR_TOKEN", "")
        self._streams: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._capturing_header: dict[str, bool] = {}
        self._saw_separator: dict[str, bool] = {}
        self._header_lines: dict[str, list[str]] = {}
        self._recent_output: dict[str, deque[str]] = {}
        self._skip_next_tokens_line: dict[str, bool] = {}
        self._last_output: dict[str, str] = {}
        self._recent_prompts: dict[str, deque[str]] = {}
        self._in_thinking: dict[str, bool] = {}
        self._output_locks: dict[str, asyncio.Lock] = {}

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
        self._capturing_header.pop(session_id, None)
        self._saw_separator.pop(session_id, None)
        self._header_lines.pop(session_id, None)
        self._recent_output.pop(session_id, None)
        self._skip_next_tokens_line.pop(session_id, None)
        self._last_output.pop(session_id, None)
        self._recent_prompts.pop(session_id, None)
        self._in_thinking.pop(session_id, None)
        self._output_locks.pop(session_id, None)
        if prompt:
            self._remember_prompt(session_id, prompt)
        payload = {
            "session_id": session_id,
            "prompt": prompt,
            "approval_choice": approval_choice,
        }
        await self._post_json("/sessions/start", payload)
        self._ensure_stream(session_id)

    async def send_input(self, session_id: str, text: str) -> None:
        """Send follow-up input to the sidecar.

        Args:
            session_id: Internal session identifier.
            text: Follow-up input to send.
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        if text:
            self._remember_prompt(session_id, text)
        payload = {"session_id": session_id, "text": text}
        await self._post_json("/sessions/input", payload)

    async def stop(self, session_id: str) -> int | None:
        """Stop the sidecar session and clean up local parsing state.

        Args:
            session_id: Internal session identifier.
        """
        store.request_stop(session_id)
        payload = {"session_id": session_id}
        await self._post_json("/sessions/stop", payload)
        task = self._streams.pop(session_id, None)
        if task:
            task.cancel()
        self._capturing_header.pop(session_id, None)
        self._saw_separator.pop(session_id, None)
        self._header_lines.pop(session_id, None)
        self._recent_output.pop(session_id, None)
        self._skip_next_tokens_line.pop(session_id, None)
        self._last_output.pop(session_id, None)
        self._recent_prompts.pop(session_id, None)
        self._in_thinking.pop(session_id, None)
        self._output_locks.pop(session_id, None)
        return None

    def _ensure_stream(self, session_id: str) -> None:
        """Start SSE consumption for a session if not already running."""
        if session_id in self._streams:
            return
        self._streams[session_id] = asyncio.create_task(self._consume_stream(session_id))

    async def _consume_stream(self, session_id: str) -> None:
        """Consume sidecar SSE events in a background thread.

        Args:
            session_id: Internal session identifier.
        """
        try:
            await asyncio.to_thread(self._stream_worker, session_id)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.exception("Sidecar stream failed", session_id=session_id)
            await self._events.on_error(session_id, "STREAM_ERROR", f"Sidecar stream failed: {exc}")

    def _stream_worker(self, session_id: str) -> None:
        """Blocking SSE reader that forwards events to the asyncio loop.

        Args:
            session_id: Internal session identifier.
        """
        import socket

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
            self._dispatch(self._events.on_error(session_id, "CONNECTION_ERROR", f"Sidecar connection failed: {exc}"))
            conn.close()
            return
        if resp.status != 200:
            data = resp.read().decode("utf-8", errors="replace")
            logger.error("Sidecar SSE failed", session_id=session_id, status=resp.status, body=data)
            self._dispatch(self._events.on_error(session_id, "SIDECAR_ERROR", f"Sidecar returned {resp.status}"))
            conn.close()
            return
        # Set per-read timeout on the socket (60s to allow for heartbeat intervals)
        read_timeout = float(os.environ.get("SIDECAR_READ_TIMEOUT_SECONDS", "60"))
        if conn.sock:
            conn.sock.settimeout(read_timeout)
        try:
            while True:
                try:
                    line = resp.fp.readline().decode("utf-8", errors="replace")
                except socket.timeout:
                    logger.warning("Sidecar read timeout", session_id=session_id, timeout_s=read_timeout)
                    self._dispatch(self._events.on_error(session_id, "READ_TIMEOUT", "Sidecar stream timed out"))
                    break
                if not line:
                    # Empty line means connection closed
                    logger.info("Sidecar stream closed", session_id=session_id)
                    break
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: ") :].strip()
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                self._handle_event(session_id, event)
        finally:
            conn.close()

    def _handle_event(self, session_id: str, event: dict) -> None:
        """Route sidecar events to the event callbacks.

        Args:
            session_id: Internal session identifier.
            event: Parsed event payload from the sidecar stream.
        """
        event_type = event.get("type")
        data = event.get("data", {})
        if event_type == "header":
            text = data.get("text", "")
            if text:
                self._dispatch(
                    self._events.on_output(
                        session_id, "combined", text, kind="header", is_final=None
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
                self._handle_output(session_id, text)
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

    def _dispatch(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Schedule an event callback on the agent's asyncio loop.

        Args:
            coro: Coroutine to schedule on the main loop.
        """
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _handle_output(self, session_id: str, text: str) -> None:
        """Parse raw output for session id/header and forward clean output.

        Args:
            session_id: Internal session identifier.
            text: Raw output chunk from the sidecar.
        """
        lock = self._output_locks.get(session_id)
        if not lock:
            lock = asyncio.Lock()
            self._output_locks[session_id] = lock
        async def _run() -> None:
            async with lock:
                for line in text.splitlines(keepends=True):
                    if not line:
                        continue
                    raw = line.rstrip("\n")
                    session = store.get_session(session_id)
                    if not session or session.state != SessionState.RUNNING:
                        return
                    if self._skip_next_tokens_line.pop(session_id, False):
                        if re.fullmatch(r"[0-9][0-9,]*", raw.strip().lower()):
                            value = int(raw.strip().replace(",", ""))
                            self._dispatch(
                                self._events.on_metadata(
                                    session_id, "tokens_used", value, raw.strip()
                                )
                            )
                            continue
                    if raw.strip().lower().startswith("tokens used"):
                        match = re.search(r"([0-9][0-9,]*)", raw)
                        if match:
                            value = int(match.group(1).replace(",", ""))
                            self._dispatch(
                                self._events.on_metadata(
                                    session_id, "tokens_used", value, match.group(1)
                                )
                            )
                            continue
                    match = SESSION_ID_RE.search(raw)
                    if match and not store.get_runner_session_id(session_id):
                        store.set_runner_session_id(session_id, match.group(1))
                        continue
                    if self._maybe_capture_header(session_id, raw):
                        continue
                    if self._should_skip_line(session_id, raw):
                        continue
                    normalized = self._normalize_output(raw)
                    if not normalized:
                        continue
                    if self._last_output.get(session_id) == normalized:
                        continue
                    if self._seen_recently(session_id, normalized):
                        continue
                    self._last_output[session_id] = normalized
                    kind = "step" if self._in_thinking.pop(session_id, False) else "final"
                    is_final = kind == "final"
                    self._dispatch(
                        self._events.on_output(
                            session_id, "combined", line, kind=kind, is_final=is_final
                        )
                    )
        self._dispatch(_run())

    def _maybe_capture_header(self, session_id: str, line: str) -> bool:
        """Capture the Codex header block and emit it as a header event.

        Args:
            session_id: Internal session identifier.
            line: Raw output line.
        """
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
                self._dispatch(
                    self._events.on_output(session_id, "combined", header, kind="header", is_final=None)
                )
                self._capturing_header[session_id] = False
                self._saw_separator[session_id] = False
                self._header_lines.pop(session_id, None)
            else:
                self._saw_separator[session_id] = True
        return True

    def _should_skip_line(self, session_id: str, line: str) -> bool:
        """Return True if a line is non-user output or prompt echo.

        Args:
            session_id: Internal session identifier.
            line: Raw output line.
        """
        lower = line.strip().lower()
        if self._skip_next_tokens_line.pop(session_id, False):
            if re.fullmatch(r"[0-9][0-9,]*", lower):
                normalized_tokens = self._normalize_output(f"tokens used: {lower}")
                self._last_output[session_id] = normalized_tokens
                self._seen_recently(session_id, normalized_tokens)
                self._dispatch(
                    self._events.on_output(
                        session_id, "combined", f"tokens used: {lower}\n", kind="step", is_final=False
                    )
                )
                return True
        if lower in {"user", "codex", "assistant", "thinking"}:
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

    async def _post_json(self, path: str, payload: dict) -> None:
        """POST JSON to the sidecar and raise on non-2xx responses.

        Args:
            path: Sidecar API path, e.g. "/sessions/start".
            payload: JSON body to send.
        """
        url = urllib.parse.urlparse(self._base_url)
        conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=10)
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Sidecar-Token"] = self._token
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", errors="replace")
        conn.close()
        if resp.status < 200 or resp.status >= 300:
            raise RuntimeError(f"Sidecar request failed: {resp.status} {data}")
