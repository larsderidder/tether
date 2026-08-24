"""Tests for bridge glue helpers."""

from types import SimpleNamespace

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


@pytest.mark.anyio
async def test_send_input_preserves_interactive_approval_when_starting(
    monkeypatch,
) -> None:
    """The first bridge message must not replace interactive approval with full auto."""

    requests: list[tuple[str, dict]] = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs["json"]))
            request = httpx.Request("POST", url)
            if url.endswith("/input"):
                return httpx.Response(
                    409,
                    request=request,
                    json={
                        "error": {
                            "code": "INVALID_STATE",
                            "message": "Session has not started",
                        }
                    },
                )
            return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        "tether.store.store.get_session",
        lambda _session_id: SimpleNamespace(approval_mode=0),
    )

    await glue._send_input("sess_123", "hello")

    assert requests[-1][1]["approval_choice"] == 0
