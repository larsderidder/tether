"""Focused tests for session model switching."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from tether.bridges import model_api
from tether.models import SessionState
from tether.runner.base import RunnerUnavailableError
from tether.store import SessionStore


@pytest.mark.anyio
async def test_model_update_rejects_unconfigured_choice(
    api_client: httpx.AsyncClient,
    fresh_store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured model lists reject typos and preserve the old model."""
    monkeypatch.setenv("TETHER_PI_DEFAULT_MODEL", "openai/gpt-5")
    monkeypatch.setenv("TETHER_PI_MODELS", "openai/gpt-5,openai/gpt-4")
    monkeypatch.delenv("TETHER_PI_BLOCKED_MODELS", raising=False)
    monkeypatch.delenv("TETHER_PI_MODEL_BLACKLIST", raising=False)

    create_resp = await api_client.post(
        "/api/sessions",
        json={
            "repo_id": "test_repo",
            "adapter": "pi_rpc",
            "model": "openai/gpt-5",
        },
    )
    assert create_resp.status_code == 201
    session_id = create_resp.json()["id"]

    patch_resp = await api_client.patch(
        f"/api/sessions/{session_id}/model",
        json={"model": "gpt-typo"},
    )

    assert patch_resp.status_code == 422
    assert patch_resp.json()["error"]["code"] == "MODEL_NOT_AVAILABLE"
    assert fresh_store.get_session(session_id).model == "openai/gpt-5"


@pytest.mark.anyio
async def test_pi_model_update_keeps_old_model_when_runner_reset_fails(
    api_client: httpx.AsyncClient,
    fresh_store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pi model changes are persisted only after the idle runner resets."""
    monkeypatch.setenv("TETHER_PI_DEFAULT_MODEL", "openai/gpt-5")
    monkeypatch.setenv("TETHER_PI_MODELS", "openai/gpt-5,openai/gpt-4")

    create_resp = await api_client.post(
        "/api/sessions",
        json={
            "repo_id": "test_repo",
            "adapter": "pi_rpc",
            "model": "openai/gpt-5",
        },
    )
    assert create_resp.status_code == 201
    session_id = create_resp.json()["id"]
    session = fresh_store.get_session(session_id)
    session.state = SessionState.AWAITING_INPUT
    fresh_store.update_session(session)

    runner = AsyncMock()
    runner.stop.side_effect = RunnerUnavailableError("stdin closed")
    monkeypatch.setattr("tether.api.sessions.get_api_runner", lambda _adapter: runner)

    patch_resp = await api_client.patch(
        f"/api/sessions/{session_id}/model",
        json={"model": "openai/gpt-4"},
    )

    assert patch_resp.status_code == 503
    assert patch_resp.json()["error"]["code"] == "MODEL_SWITCH_FAILED"
    assert fresh_store.get_session(session_id).model == "openai/gpt-5"
    runner.stop.assert_awaited_once_with(session_id)


@pytest.mark.anyio
async def test_bridge_model_api_reports_tether_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge commands surface the API error instead of a bare HTTP status."""
    response = httpx.Response(
        422,
        json={
            "error": {
                "code": "MODEL_NOT_AVAILABLE",
                "message": "Model 'typo' is not configured for pi_rpc.",
            }
        },
        request=httpx.Request("PATCH", "http://test/api/sessions/sess_1/model"),
    )

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def patch(self, *args, **kwargs):
            return response

    monkeypatch.setattr(model_api.httpx, "AsyncClient", FakeClient)

    with pytest.raises(model_api.ModelApiError) as exc_info:
        await model_api.set_session_model("sess_1", "typo")

    assert "Model 'typo' is not configured for pi_rpc." in str(exc_info.value)
    assert "MODEL_NOT_AVAILABLE" in str(exc_info.value)


@pytest.mark.anyio
async def test_invalid_model_creation_does_not_leave_a_session(
    api_client: httpx.AsyncClient,
    fresh_store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject model typos before creating any persistent session."""
    monkeypatch.setenv("TETHER_PI_MODELS", "provider/known")
    monkeypatch.setenv("TETHER_PI_DEFAULT_MODEL", "provider/known")

    response = await api_client.post(
        "/api/sessions", json={"adapter": "pi_rpc", "model": "provider/typo"}
    )

    assert response.status_code == 422
    assert fresh_store.list_sessions() == []
