"""SSE and HTTP client for agent communication."""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import aiohttp

logger = logging.getLogger(__name__)


class AgentClient:
    """Async HTTP client for the Tether agent API.

    Provides methods for listing sessions, subscribing to SSE events,
    sending input, and stopping sessions.

    Args:
        base_url: Agent base URL (e.g., "http://localhost:8787").
        token: Bearer token for authentication (empty string if auth disabled).
    """

    def __init__(self, base_url: str, token: str):
        self._base_url = base_url
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """Initialize HTTP session."""
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._session = aiohttp.ClientSession(headers=headers)

    async def stop(self) -> None:
        """Close HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    async def subscribe(self, session_id: str) -> AsyncIterator[dict]:
        """Subscribe to SSE events for a session.

        Yields parsed event dictionaries.
        """
        if not self._session:
            raise RuntimeError("AgentClient not started")

        url = f"{self._base_url}/events/sessions/{session_id}"
        try:
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"SSE subscription failed: {resp.status} {text}")

                async for line in resp.content:
                    line_str = line.decode("utf-8").strip()
                    if not line_str or line_str.startswith(":"):
                        continue
                    if not line_str.startswith("data: "):
                        continue
                    payload = line_str[6:]
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON in SSE: %s", payload[:100])
        except aiohttp.ClientError as e:
            logger.error("SSE connection error for %s: %s", session_id, e)
            raise

    async def list_sessions(self) -> list[dict]:
        """Fetch all sessions from the agent."""
        if not self._session:
            raise RuntimeError("AgentClient not started")

        url = f"{self._base_url}/api/sessions"
        async with self._session.get(url) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Failed to list sessions: {resp.status} {text}")
            data = await resp.json()
            return data.get("sessions", [])

    async def get_session(self, session_id: str) -> dict | None:
        """Fetch a single session by ID."""
        if not self._session:
            raise RuntimeError("AgentClient not started")

        url = f"{self._base_url}/api/sessions/{session_id}"
        async with self._session.get(url) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Failed to get session: {resp.status} {text}")
            data = await resp.json()
            return data.get("session")

    async def send_input(self, session_id: str, text: str) -> dict:
        """Send input to a session."""
        if not self._session:
            raise RuntimeError("AgentClient not started")

        url = f"{self._base_url}/api/sessions/{session_id}/input"
        async with self._session.post(url, json={"text": text}) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"Input failed: {resp.status} {body}")
            return await resp.json()

    async def interrupt_session(self, session_id: str) -> dict:
        """Interrupt the current turn in a session."""
        if not self._session:
            raise RuntimeError("AgentClient not started")

        url = f"{self._base_url}/api/sessions/{session_id}/interrupt"
        async with self._session.post(url) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"Interrupt failed: {resp.status} {body}")
            return await resp.json()

    async def list_external_sessions(
        self,
        directory: str | None = None,
        runner_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Fetch external sessions (Claude Code, Codex) that can be attached."""
        if not self._session:
            raise RuntimeError("AgentClient not started")

        params = {}
        if directory:
            params["directory"] = directory
        if runner_type:
            params["runner_type"] = runner_type
        if limit:
            params["limit"] = str(limit)

        url = f"{self._base_url}/api/external-sessions"
        async with self._session.get(url, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Failed to list external sessions: {resp.status} {text}")
            return await resp.json()

    async def attach_to_external_session(
        self,
        external_id: str,
        runner_type: str,
        directory: str,
    ) -> dict:
        """Attach to an external session (Claude Code or Codex)."""
        if not self._session:
            raise RuntimeError("AgentClient not started")

        url = f"{self._base_url}/api/sessions/attach"
        payload = {
            "external_id": external_id,
            "runner_type": runner_type,
            "directory": directory,
        }
        async with self._session.post(url, json=payload) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"Attach failed: {resp.status} {body}")
            return await resp.json()
