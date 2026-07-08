"""Helpers for bridge model commands."""

from __future__ import annotations

import httpx

from tether.settings import settings


def _api_headers() -> dict[str, str]:
    """Return internal API auth headers."""
    token = settings.token()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def get_session_model(session_id: str) -> dict:
    """Fetch active model information for a session."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}/model",
            headers=_api_headers(),
            timeout=10.0,
        )
        response.raise_for_status()
    return response.json()


async def set_session_model(session_id: str, model: str) -> dict:
    """Set the model used for future turns in a session."""
    async with httpx.AsyncClient() as client:
        response = await client.patch(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}/model",
            json={"model": model},
            headers=_api_headers(),
            timeout=10.0,
        )
        response.raise_for_status()
    return response.json()


def format_model_info(info: dict) -> str:
    """Render model information for text bridges."""
    adapter = info.get("adapter") or "default"
    active = info.get("model") or "not set"
    default = info.get("default_model") or "not set"
    models = [str(item) for item in info.get("available_models") or []]
    lines = [
        f"Adapter: {adapter}",
        f"Active model: {active}",
        f"Default model: {default}",
    ]
    if models:
        lines.append("Available models:")
        for model in models:
            marker = "*" if model == active else " "
            lines.append(f"{marker} {model}")
    else:
        lines.append("No model list configured for this adapter.")
    return "\n".join(lines)
