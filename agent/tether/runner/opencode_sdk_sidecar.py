"""Runner adapter that talks directly to the OpenCode native REST API.

OpenCode exposes its own HTTP server (``opencode serve``) with these endpoints:

- ``POST /session``                                    create a session
- ``POST /session/{id}/prompt_async``                  send a message (fire-and-forget 204)
- ``POST /session/{id}/abort``                         interrupt
- ``POST /session/{id}/permissions/{permissionID}``    respond to a permission request
- ``GET  /global/event``                               SSE stream of all events

Events are JSON lines:  ``{"directory": "...", "payload": {"type": "...", "properties": {...}}}``

Relevant payload types:
- ``message.part.delta``    streaming text delta  (properties: sessionID, partID, field, delta)
- ``message.part.updated``  part finalized        (properties: part{type, text, ...})
- ``session.status``        busy / idle / retry   (properties: sessionID, status{type})
- ``session.idle``          turn done             (properties: sessionID)
- ``session.error``         error                 (properties: sessionID, error)
- ``permission.updated``    permission request    (properties: Permission{id, title, sessionID, ...})
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import shutil
import socket
import urllib.parse
from pathlib import Path
from typing import Any, Coroutine

import httpx
import structlog

from tether.runner.base import RunnerEvents
from tether.runner.base import RunnerUnavailableError
from tether.runner.opencode_sidecar_manager import ensure_opencode_sidecar_started
from tether.settings import settings
from tether.store import store

logger = structlog.get_logger(__name__)


def _find_opencode_bin() -> str | None:
    """Find the opencode binary.

    Checks (in order):
    1. OPENCODE_BIN env var
    2. ~/.opencode/bin/opencode
    3. opencode in PATH
    """
    env_bin = os.environ.get("OPENCODE_BIN")
    if env_bin:
        path = Path(env_bin).expanduser()
        if path.exists() and path.is_file():
            return str(path)

    home_bin = Path.home() / ".opencode" / "bin" / "opencode"
    if home_bin.exists():
        return str(home_bin)

    path_bin = shutil.which("opencode")
    if path_bin:
        return path_bin

    return None


class OpenCodeSidecarRunner:
    """Runner that talks to the OpenCode native HTTP server."""

    runner_type: str = "opencode"

    def __init__(self, events: RunnerEvents) -> None:
        self._events = events
        self._base_url = settings.opencode_sidecar_url()
        self._token = settings.opencode_sidecar_token()
        self._streams: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        # Map tether session_id -> opencode session ID
        self._opencode_session_ids: dict[str, str] = {}
        # Track which text parts are being accumulated per (session_id, part_id)
        self._part_texts: dict[tuple[str, str], str] = {}

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

        # Re-use an existing opencode session if one is attached.
        oc_session_id = store.get_runner_session_id(session_id)

        if not oc_session_id:
            # Create a new opencode session scoped to the working directory.
            oc_session_id = await self._create_session(workdir=workdir)
            store.set_runner_session_id(session_id, oc_session_id)
            logger.info(
                "Created OpenCode session",
                session_id=session_id,
                opencode_session_id=oc_session_id,
            )
        else:
            logger.info(
                "Reusing existing OpenCode session",
                session_id=session_id,
                opencode_session_id=oc_session_id,
            )

        self._opencode_session_ids[session_id] = oc_session_id

        await self._events.on_header(
            session_id,
            title="OpenCode",
            model=None,
            provider="opencode",
            thread_id=oc_session_id,
        )

        self._ensure_stream(session_id)
        await self._send_prompt(oc_session_id, prompt)

    async def send_input(self, session_id: str, text: str) -> None:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        oc_session_id = self._opencode_session_ids.get(
            session_id
        ) or store.get_runner_session_id(session_id)
        if not oc_session_id:
            raise RuntimeError(
                "No OpenCode session attached. Start a session before sending input."
            )

        self._opencode_session_ids[session_id] = oc_session_id
        self._ensure_stream(session_id)
        await self._send_prompt(oc_session_id, text)

    async def stop(self, session_id: str) -> int | None:
        store.request_stop(session_id)
        oc_session_id = self._opencode_session_ids.get(session_id)
        if oc_session_id:
            try:
                await self._post_json(f"/session/{oc_session_id}/abort", {})
            except Exception:
                logger.warning(
                    "Failed to abort OpenCode session", session_id=session_id
                )
        task = self._streams.pop(session_id, None)
        if task:
            task.cancel()
        return None

    def update_permission_mode(self, session_id: str, approval_choice: int) -> None:
        """OpenCode manages its own permission policy; nothing to do here."""
        return

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
                    "OpenCode stream failed", session_id=session_id
                )
                await self._events.on_error(
                    session_id,
                    "STREAM_ERROR",
                    f"OpenCode stream failed: {exc}",
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
        """Blocking SSE consumer. Runs in a thread."""
        url = urllib.parse.urlparse(self._base_url)
        conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=30)
        headers = {}
        if self._token:
            headers["X-Sidecar-Token"] = self._token

        try:
            conn.request("GET", "/global/event", headers=headers)
            resp = conn.getresponse()
        except (socket.timeout, OSError) as exc:
            logger.error(
                "OpenCode connection failed",
                session_id=session_id,
                error=str(exc),
            )
            self._dispatch(
                self._events.on_error(
                    session_id,
                    "CONNECTION_ERROR",
                    f"OpenCode connection failed: {exc}",
                )
            )
            conn.close()
            return True

        if resp.status != 200:
            data = resp.read().decode("utf-8", errors="replace")
            logger.error(
                "OpenCode SSE endpoint returned error",
                session_id=session_id,
                status=resp.status,
                body=data[:200],
            )
            self._dispatch(
                self._events.on_error(
                    session_id,
                    "SIDECAR_ERROR",
                    f"OpenCode SSE returned {resp.status}",
                )
            )
            conn.close()
            return resp.status >= 500

        read_timeout = 60.0
        if conn.sock:
            conn.sock.settimeout(read_timeout)

        oc_session_id = self._opencode_session_ids.get(session_id)

        try:
            while True:
                if store.is_stop_requested(session_id):
                    return False

                try:
                    line = resp.fp.readline().decode("utf-8", errors="replace")
                except socket.timeout:
                    logger.warning(
                        "OpenCode stream read timeout",
                        session_id=session_id,
                        timeout_s=read_timeout,
                    )
                    return True

                if not line:
                    logger.info("OpenCode stream closed", session_id=session_id)
                    return True

                if not line.startswith("data: "):
                    continue

                payload_str = line[len("data: "):].strip()
                try:
                    envelope = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue

                event = envelope.get("payload", {})
                event_type = event.get("type", "")
                props = event.get("properties", {})

                # Filter to events for this opencode session only.
                event_session_id = props.get("sessionID") or props.get(
                    "info", {}
                ).get("sessionID") or props.get("part", {}).get("sessionID")

                if oc_session_id and event_session_id and event_session_id != oc_session_id:
                    continue

                self._handle_event(session_id, event_type, props)
        finally:
            conn.close()

        return True

    def _handle_event(self, session_id: str, event_type: str, props: dict) -> None:
        if event_type == "message.part.delta":
            # Streaming text delta
            field = props.get("field", "")
            delta = props.get("delta", "")
            if field == "text" and delta:
                self._dispatch(
                    self._events.on_output(
                        session_id,
                        stream="combined",
                        text=delta,
                        kind="text",
                        is_final=False,
                    )
                )
            return

        if event_type == "message.part.updated":
            part = props.get("part", {})
            part_type = part.get("type", "")

            if part_type == "step-finish":
                # Turn complete - emit token/cost metadata and mark final output.
                tokens = part.get("tokens", {})
                cost = part.get("cost", 0)

                if tokens.get("input") or tokens.get("output"):
                    self._dispatch(
                        self._events.on_metadata(
                            session_id, "tokens_input", tokens.get("input", 0), raw=tokens
                        )
                    )
                    self._dispatch(
                        self._events.on_metadata(
                            session_id, "tokens_output", tokens.get("output", 0), raw=tokens
                        )
                    )
                    self._dispatch(
                        self._events.on_metadata(
                            session_id, "tokens_total", tokens.get("total", 0), raw=tokens
                        )
                    )
                if cost:
                    self._dispatch(
                        self._events.on_metadata(
                            session_id, "cost", cost, raw={"cost": cost}
                        )
                    )

                self._dispatch(
                    self._events.on_output(
                        session_id,
                        stream="combined",
                        text="",
                        kind="text",
                        is_final=True,
                    )
                )
            return

        if event_type == "session.status":
            status = props.get("status", {})
            status_type = status.get("type", "")
            if status_type == "idle":
                self._dispatch(self._events.on_awaiting_input(session_id))
            elif status_type == "busy":
                elapsed = 0.0
                self._dispatch(
                    self._events.on_heartbeat(session_id, elapsed_s=elapsed, done=False)
                )
            return

        if event_type == "session.idle":
            self._dispatch(self._events.on_awaiting_input(session_id))
            return

        if event_type == "session.error":
            error = props.get("error", {})
            message = (error.get("data") or {}).get("message", str(error))
            self._dispatch(
                self._events.on_error(session_id, "SESSION_ERROR", message)
            )
            return

        if event_type == "permission.updated":
            # props is the Permission object directly
            perm_id = props.get("id", "")
            title = props.get("title", "Permission request")
            perm_type = props.get("type", "")
            pattern = props.get("pattern")
            metadata = props.get("metadata", {})

            description = title
            if pattern:
                pat_str = (
                    ", ".join(pattern) if isinstance(pattern, list) else str(pattern)
                )
                description = f"{title}: {pat_str}"

            self._dispatch(
                self._events.on_permission_request(
                    session_id,
                    request_id=perm_id,
                    tool_name=perm_type,
                    tool_input=metadata,
                    description=description,
                )
            )
            return

        if event_type == "message.updated":
            # Emit a header update when we first learn the model/provider.
            info = props.get("info", {})
            if info.get("role") == "assistant" and info.get("modelID"):
                self._dispatch(
                    self._events.on_header(
                        session_id,
                        title="OpenCode",
                        model=info.get("modelID"),
                        provider=info.get("providerID"),
                    )
                )
            return

    def _dispatch(self, coro: Coroutine[Any, Any, Any]) -> None:
        if not self._loop:
            logger.warning("Cannot dispatch OpenCode event: event loop not set")
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _create_session(self, workdir: str | None = None) -> str:
        """Create a new OpenCode session and return its ID."""
        body: dict = {}
        params = ""
        if workdir:
            params = f"?directory={urllib.parse.quote(workdir)}"

        status, data = await self._post_once(f"/session{params}", body)
        if status < 200 or status >= 300:
            raise RuntimeError(f"Failed to create OpenCode session: {status} {data}")
        try:
            return json.loads(data)["id"]
        except (json.JSONDecodeError, KeyError) as exc:
            raise RuntimeError(
                f"Unexpected response from OpenCode session create: {data!r}"
            ) from exc

    async def _send_prompt(self, oc_session_id: str, text: str) -> None:
        """Send a prompt to an opencode session (prompt_async - returns 204)."""
        payload = {
            "parts": [{"type": "text", "text": text}],
        }
        await self._post_json(f"/session/{oc_session_id}/prompt_async", payload)

    async def _post_json(self, path: str, payload: dict) -> None:
        url = urllib.parse.urlparse(self._base_url)
        if not url.hostname:
            raise RunnerUnavailableError(
                f"Invalid OpenCode sidecar URL: {self._base_url}"
            )
        try:
            status, data = await self._post_once(path, payload)
        except (httpx.HTTPError, OSError, socket.timeout) as exc:
            if settings.opencode_sidecar_managed():
                await ensure_opencode_sidecar_started()
                status, data = await self._post_once(path, payload)
            else:
                raise RunnerUnavailableError(
                    f"OpenCode is not reachable at {self._base_url} ({exc}). "
                    "Start OpenCode or check TETHER_OPENCODE_SIDECAR_URL."
                ) from exc

        if status < 200 or status >= 300:
            raise RuntimeError(f"OpenCode request failed: {status} {data}")

    async def _post_once(self, path: str, payload: dict) -> tuple[int, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Sidecar-Token"] = self._token

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._base_url}{path}",
                json=payload,
                headers=headers,
            )
            return response.status_code, response.text
