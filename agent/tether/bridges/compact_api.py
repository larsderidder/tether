"""Shared bridge helper for requesting runner compaction."""

from __future__ import annotations

import httpx

from tether.settings import settings


def _api_headers() -> dict[str, str]:
    token = settings.token()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


async def compact_session(
    session_id: str, custom_instructions: str | None = None
) -> dict:
    """Request compaction for a Tether session."""

    payload = {}
    if custom_instructions:
        payload["custom_instructions"] = custom_instructions

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}/compact",
            json=payload,
            headers=_api_headers(),
            timeout=30.0,
        )
    if response.is_error:
        try:
            data = response.json()
            message = (data.get("error") or {}).get("message") or response.text
        except Exception:
            message = response.text
        raise RuntimeError(message or f"Compaction failed ({response.status_code})")
    return response.json()
