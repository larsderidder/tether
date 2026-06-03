"""Tests for bridge glue helpers."""

import httpx
import pytest

from tether.bridges import glue


@pytest.mark.anyio
async def test_sync_session_reports_tether_error_message(monkeypatch) -> None:
    """Sync failures should show the API error instead of a generic HTTP error."""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, *args, **kwargs):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "INVALID_STATE",
                        "message": "Session is not attached to an external session",
                        "details": None,
                    }
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(RuntimeError) as exc_info:
        await glue._sync_session("sess_123")

    assert "Session is not attached to an external session" in str(exc_info.value)
    assert "INVALID_STATE" in str(exc_info.value)
