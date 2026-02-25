"""Runner adapter that delegates execution to the OpenCode SDK sidecar service.

The sidecar wraps the OpenCode SDK and exposes the standard Tether sidecar
protocol:

    POST /sessions/start      - Start a session or send first prompt
    POST /sessions/input      - Send follow-up input
    POST /sessions/interrupt  - Abort the running turn
    GET  /events/:sessionId   - SSE stream

This runner is structurally identical to the Codex sidecar runner. All
OpenCode-specific logic lives in the TypeScript sidecar.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import urllib.parse
from typing import Any, Coroutine

import structlog

from tether.runner.base import RunnerEvents
from tether.runner.base import RunnerUnavailableError
from tether.runner.opencode_sidecar_manager import ensure_opencode_sidecar_started
from tether.settings import settings
from tether.store import store

logger = structlog.get_logger(__name__)


class OpenCodeSidecarRunner:
    """Runner that delegates OpenCode execution to a local TypeScript sidecar."""

    runner_type: str = "opencode"

    def __init__(self, events: RunnerEvents) -> None:
        self._events = events
        self._base_url = settings.opencode_sidecar_url()
        self._token = settings.opencode_sidecar_token()
        self._streams: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Runner protocol
    # ------------------------------------------------------------------

    async def start(self, session_id: str, prompt: str, approval_choice: int) -> None:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        if settings.opencode_sidecar_managed():
            await ensure_opencode_sidecar_started()
        store.clear_stop_requested(session_id)

        workdir = store.get_workdir(session_id)
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
        if thread_id:
            payload["thread_id"] = thread_id

        await self._post_json("/sessions/start", payload)
        self._ensure_stream(session_id)

    async def send_input(self, session_id: str, text: str) -> None:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        payload = {"session_id": session_id, "text": text}
        await self._post_json("/sessions/input", payload)
        self._ensure_stream(session_id)

    async def stop(self, session_id: str) -> int | None:
        store.request_stop(session_id)
        payload = {"session_id": session_id}
        await self._post_json("/sessions/interrupt", payload)
        task = self._streams.pop(session_id, None)
        if task:
            task.cancel()
        return None

    def update_permission_mode(self, session_id: str, approval_choice: int) -> None:
        """OpenCode sidecar owns the permission policy; nothing to do here."""

    # ------------------------------------------------------------------
    # SSE stream
    # ------------------------------------------------------------------

    def _ensure_stream(self, session_id: str) -> None:
        existing = self._streams.get(session_id)
        if existing and not existing.done():
            return
        if existing and existing.done():
            self._streams.pop(session_id, None)
        self._streams[session_id] = asyncio.create_task(
            self._consume_stream(session_id)
        )

    async def _consume_stream(self, session_id: str) -> None:
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
                logger.exception(
                    "OpenCode sidecar stream failed", session_id=session_id
                )
                await self._events.on_error(
                    session_id,
                    "STREAM_ERROR",
                    f"OpenCode sidecar stream failed: {exc}",
                )
                should_retry = True
            if not should_retry:
                return
            if settings.opencode_sidecar_managed():
                try:
                    await ensure_opencode_sidecar_started()
                except Exception:
                    logger.exception(
                        "Failed to restart managed OpenCode sidecar",
                        session_id=session_id,
                    )
            await asyncio.sleep(backoff_s)
            backoff_s = min(max_backoff_s, backoff_s * 2)

    def _stream_worker(self, session_id: str) -> bool:
        """Blocking SSE reader that forwards events to the asyncio loop."""
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
            logger.error(
                "OpenCode sidecar connection failed",
                session_id=session_id,
                error=str(exc),
            )
            self._dispatch(
                self._events.on_error(
                    session_id,
                    "CONNECTION_ERROR",
                    f"OpenCode sidecar connection failed: {exc}",
                )
            )
            conn.close()
            return True

        if resp.status != 200:
            data = resp.read().decode("utf-8", errors="replace")
            logger.error(
                "OpenCode sidecar SSE failed",
                session_id=session_id,
                status=resp.status,
                body=data[:200],
            )
            self._dispatch(
                self._events.on_error(
                    session_id,
                    "SIDECAR_ERROR",
                    f"OpenCode sidecar returned {resp.status}",
                )
            )
            conn.close()
            return resp.status >= 500

        read_timeout = 60.0
        if conn.sock:
            conn.sock.settimeout(read_timeout)

        try:
            while True:
                try:
                    line = resp.fp.readline().decode("utf-8", errors="replace")
                except socket.timeout:
                    logger.warning(
                        "OpenCode sidecar read timeout",
                        session_id=session_id,
                        timeout_s=read_timeout,
                    )
                    self._dispatch(
                        self._events.on_error(
                            session_id,
                            "READ_TIMEOUT",
                            "OpenCode sidecar stream timed out",
                        )
                    )
                    return True

                if not line:
                    logger.info(
                        "OpenCode sidecar stream closed", session_id=session_id
                    )
                    return True

                if not line.startswith("data: "):
                    continue

                payload = line[len("data: "):].strip()
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Failed to parse SSE event",
                        session_id=session_id,
                        payload=payload[:200],
                        error=str(exc),
                    )
                    continue

                logger.debug(
                    "Received sidecar event",
                    session_id=session_id,
                    event_type=event.get("type"),
                )
                self._handle_event(session_id, event)
        finally:
            conn.close()

        return True

    def _handle_event(self, session_id: str, event: dict) -> None:
        """Route sidecar events to the event callbacks."""
        event_type = event.get("type")
        data = event.get("data", {})

        if event_type == "header":
            title = data.get("title", "OpenCode")
            model = data.get("model")
            provider = data.get("provider")
            thread_id = data.get("thread_id")
            self._dispatch(
                self._events.on_header(
                    session_id,
                    title=title,
                    model=model,
                    provider=provider,
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
                        session_id,
                        "combined",
                        text,
                        kind=kind,
                        is_final=is_final,
                    )
                )
            else:
                logger.warning(
                    "Sidecar output missing kind field", session_id=session_id
                )
            return

        if event_type == "metadata":
            key = data.get("key")
            value = data.get("value")
            raw = data.get("raw", "")
            if key:
                self._dispatch(
                    self._events.on_metadata(session_id, key, value, raw)
                )
            return

        if event_type == "heartbeat":
            elapsed_s = float(data.get("elapsed_s", 0.0))
            done = bool(data.get("done", False))
            self._dispatch(self._events.on_heartbeat(session_id, elapsed_s, done))
            return

        if event_type == "error":
            code = data.get("code", "INTERNAL_ERROR")
            message = data.get("message", "Unknown error")
            self._dispatch(self._events.on_error(session_id, code, message))
            return

        if event_type == "exit":
            exit_code = data.get("exit_code")
            if store.is_stop_requested(session_id) or exit_code not in (0, None):
                self._dispatch(self._events.on_exit(session_id, exit_code))
            else:
                self._dispatch(self._events.on_awaiting_input(session_id))
            return

        logger.debug(
            "Unknown sidecar event type",
            session_id=session_id,
            event_type=event_type,
        )

    def _dispatch(self, coro: Coroutine[Any, Any, Any]) -> None:
        if not self._loop:
            logger.warning("Cannot dispatch event: event loop not set")
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _post_json(self, path: str, payload: dict) -> None:
        """POST JSON to the sidecar and raise on non-2xx responses."""
        url = urllib.parse.urlparse(self._base_url)
        if not url.hostname:
            raise RunnerUnavailableError(
                f"Invalid OpenCode sidecar URL: {self._base_url}"
            )

        try:
            status, data = await asyncio.to_thread(
                self._post_once, path, payload
            )
        except (ConnectionRefusedError, socket.timeout, OSError) as exc:
            if settings.opencode_sidecar_managed():
                await ensure_opencode_sidecar_started()
                status, data = await asyncio.to_thread(
                    self._post_once, path, payload
                )
            else:
                raise RunnerUnavailableError(
                    f"OpenCode sidecar is not reachable at {self._base_url} ({exc}). "
                    "Start the sidecar or check TETHER_OPENCODE_SIDECAR_URL."
                ) from exc

        if status < 200 or status >= 300:
            raise RuntimeError(f"OpenCode sidecar request failed: {status} {data}")

    def _post_once(self, path: str, payload: dict) -> tuple[int, str]:
        """Blocking POST to the sidecar."""
        url = urllib.parse.urlparse(self._base_url)
        conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=10)
        body = json.dumps(payload).encode("utf-8")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Sidecar-Token"] = self._token
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read().decode("utf-8", errors="replace")
            return resp.status, data
        finally:
            conn.close()
