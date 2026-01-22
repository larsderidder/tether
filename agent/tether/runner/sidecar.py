"""Runner adapter that delegates execution to the local sidecar service."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import http.client
from typing import Any, Coroutine

import structlog

from tether.runner.base import RunnerEvents
from tether.store import store

logger = structlog.get_logger("tether.runner.sidecar")


class SidecarRunner:
    """Runner that delegates Codex execution to a local TypeScript sidecar."""

    runner_type: str = "codex"

    def __init__(self, events: RunnerEvents, base_url: str | None = None) -> None:
        self._events = events
        self._base_url = base_url or "http://localhost:8788"
        self._token = os.environ.get("CODEX_SDK_SIDECAR_TOKEN", "") or os.environ.get("SIDECAR_TOKEN", "")
        self._streams: dict[str, asyncio.Task] = {}
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
        payload = {"session_id": session_id, "text": text}
        await self._post_json("/sessions/input", payload)

    async def stop(self, session_id: str) -> int | None:
        """Stop the sidecar session.

        Args:
            session_id: Internal session identifier.
        """
        store.request_stop(session_id)
        payload = {"session_id": session_id}
        await self._post_json("/sessions/stop", payload)
        task = self._streams.pop(session_id, None)
        if task:
            task.cancel()
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

    def _dispatch(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Schedule an event callback on the agent's asyncio loop.

        Args:
            coro: Coroutine to schedule on the main loop.
        """
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

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
