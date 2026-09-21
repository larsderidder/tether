"""Helpers for bridge model commands."""

from __future__ import annotations

import httpx

from tether.settings import settings


class ModelApiError(RuntimeError):
    """User-facing error returned by the session model API."""


def _api_headers() -> dict[str, str]:
    """Return internal API auth headers."""
    token = settings.token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _json_or_error(response: httpx.Response) -> dict:
    """Return JSON or raise a bridge-friendly API error."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        message = _error_message(response)
        raise ModelApiError(message) from exc
    return response.json()


def _error_message(response: httpx.Response) -> str:
    """Extract a concise error message from a Tether API response."""
    try:
        data = response.json()
    except ValueError:
        return f"Tether API returned HTTP {response.status_code}"

    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        code = str(error.get("code") or "").strip()
        if message and code:
            return f"{message} ({code})"
        if message:
            return message
        if code:
            return code
    return f"Tether API returned HTTP {response.status_code}"


async def get_session_model(session_id: str) -> dict:
    """Fetch active model information for a session."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}/model",
            headers=_api_headers(),
            timeout=10.0,
        )
    return _json_or_error(response)


async def set_session_model(session_id: str, model: str) -> dict:
    """Set the model used for future turns in a session."""
    async with httpx.AsyncClient() as client:
        response = await client.patch(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}/model",
            json={"model": model},
            headers=_api_headers(),
            timeout=10.0,
        )
    return _json_or_error(response)


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
