"""Shared bridge helpers for session output policy commands."""

from __future__ import annotations

import math

import httpx

from tether.settings import settings

_VERBOSITY_LEVELS = {"none", "minimal", "medium", "high"}


def _headers() -> dict[str, str]:
    """Return internal API auth headers."""
    token = settings.token()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def get_bridge_output_policy(session_id: str) -> dict:
    """Fetch bridge output policy for a session."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}",
            headers=_headers(),
        )
        response.raise_for_status()
        return response.json()


async def set_bridge_output_policy(
    session_id: str,
    *,
    verbosity: str | None = None,
    buffer_max_seconds: float | None = None,
    clear_verbosity: bool = False,
    clear_buffer: bool = False,
) -> dict:
    """Update bridge output policy for a session."""
    body: dict[str, str | float | None] = {}
    if clear_verbosity:
        body["bridge_verbosity"] = None
    elif verbosity is not None:
        body["bridge_verbosity"] = verbosity
    if clear_buffer:
        body["bridge_buffer_max_seconds"] = None
    elif buffer_max_seconds is not None:
        body["bridge_buffer_max_seconds"] = buffer_max_seconds

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.patch(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}/bridge-output",
            headers=_headers(),
            json=body,
        )
        response.raise_for_status()
        return response.json()


def parse_verbosity_arg(raw: str) -> tuple[str | None, str | None, bool]:
    """Parse a bridge verbosity command argument."""
    value = raw.strip().lower()
    if not value:
        return None, None, False
    if value in {"default", "global", "clear", "reset"}:
        return None, None, True
    if value not in _VERBOSITY_LEVELS:
        levels = ", ".join(sorted(_VERBOSITY_LEVELS))
        return None, f"Unknown verbosity `{value}`. Use one of: {levels}.", False
    return value, None, False


def parse_buffer_arg(raw: str) -> tuple[float | None, str | None, bool]:
    """Parse a bridge buffer command argument."""
    value = raw.strip().lower()
    if not value:
        return None, None, False
    if value in {"off", "none", "default", "global", "clear", "reset"}:
        return None, None, True
    try:
        seconds = float(value.rstrip("s"))
    except ValueError:
        return None, "Buffer must be a number of seconds, or `off`.", False
    if not math.isfinite(seconds):
        return None, "Buffer must be a finite number of seconds.", False
    if seconds < 0 or seconds > 300:
        return None, "Buffer seconds must be between 0 and 300.", False
    return seconds, None, False


def format_bridge_output_policy(session: dict) -> str:
    """Render bridge output policy for chat bridges."""
    verbosity = session.get("bridge_verbosity")
    effective_verbosity = session.get("effective_bridge_verbosity") or "minimal"
    buffer_seconds = session.get("bridge_buffer_max_seconds")
    effective_buffer_seconds = session.get("effective_bridge_buffer_max_seconds")

    if verbosity is None:
        verbosity_line = f"Verbosity: {effective_verbosity} (global default)"
    else:
        verbosity_line = f"Verbosity: {effective_verbosity} (session override)"

    if buffer_seconds is None:
        if effective_buffer_seconds is None:
            buffer_line = "Buffer: until final/end turn"
        else:
            buffer_line = f"Buffer: max {effective_buffer_seconds:g}s (global default)"
    else:
        buffer_line = f"Buffer: max {buffer_seconds:g}s (session override)"

    return f"{verbosity_line}\n{buffer_line}"
