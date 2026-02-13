"""Dependency helpers for API endpoints."""

from __future__ import annotations

from fastapi import Request

from tether.api.errors import raise_http_error


async def require_token(request: Request) -> None:
    """Enforce bearer token auth when configured.

    Args:
        request: Incoming request to validate.
    """
    token = request.app.state.agent_token
    if not token:
        return
    auth = request.headers.get("authorization")
    if not auth or not auth.lower().startswith("bearer "):
        # Drain request body to avoid hanging ASGI clients on early auth failure.
        await request.body()
        raise_http_error("UNAUTHORIZED", "Missing or invalid bearer token", 401)
    value = auth.split(" ", 1)[1]
    if value != token:
        # Drain request body to avoid hanging ASGI clients on early auth failure.
        await request.body()
        raise_http_error("UNAUTHORIZED", "Missing or invalid bearer token", 401)
