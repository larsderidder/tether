"""Targeted regression tests for high-risk stability paths."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tether.bridges.manager import bridge_manager
from tether.models import SessionState


class LegacyThreadBridge:
    """Bridge implementation without existing_thread_id support."""

    def __init__(self) -> None:
        self.thread_calls: list[dict] = []
        self.outputs: list[dict] = []

    async def create_thread(self, session_id: str, session_name: str) -> dict:
        self.thread_calls.append({"session_id": session_id, "session_name": session_name})
        return {"thread_id": f"legacy_{session_id}", "platform": "legacy"}

    async def on_output(self, session_id: str, text: str, metadata: dict | None = None) -> None:
        self.outputs.append({"session_id": session_id, "text": text, "metadata": metadata})

    async def on_approval_request(self, session_id: str, request) -> None:
        return None

    async def on_status_change(self, session_id: str, status: str, metadata: dict | None = None) -> None:
        return None


@pytest.mark.anyio
async def test_create_session_platform_binding_supports_legacy_bridge_signature(
    api_client: httpx.AsyncClient,
    tmp_path,
) -> None:
    """Creating a session should work with older bridge create_thread signatures."""
    bridge = LegacyThreadBridge()
    bridge_manager.register_bridge("legacy", bridge)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    response = await api_client.post(
        "/api/sessions",
        json={
            "directory": str(repo_dir),
            "platform": "legacy",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["platform"] == "legacy"
    assert data["platform_thread_id"].startswith("legacy_sess_")
    assert len(bridge.thread_calls) == 1


@pytest.mark.anyio
async def test_attach_external_platform_binding_supports_legacy_bridge_signature(
    api_client: httpx.AsyncClient,
    monkeypatch,
    tmp_path,
) -> None:
    """Attach flow should also tolerate older bridge create_thread signatures."""
    bridge = LegacyThreadBridge()
    bridge_manager.register_bridge("legacy", bridge)

    import tether.api.external_sessions as external_sessions

    detail = SimpleNamespace(first_prompt="Test prompt", messages=[])
    monkeypatch.setattr(external_sessions, "get_external_session_detail", lambda **kwargs: detail)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    response = await api_client.post(
        "/api/sessions/attach",
        json={
            "external_id": "ext_legacy_123",
            "runner_type": "claude_code",
            "directory": str(repo_dir),
            "platform": "legacy",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["platform"] == "legacy"
    assert data["platform_thread_id"].startswith("legacy_sess_")
    assert len(bridge.thread_calls) == 1


@pytest.mark.anyio
async def test_attach_existing_external_session_replays_with_actual_runner_type(
    api_client: httpx.AsyncClient,
    monkeypatch,
    tmp_path,
) -> None:
    """Platform binding on an existing attach should use the session's runner type."""
    bridge = LegacyThreadBridge()
    bridge_manager.register_bridge("legacy", bridge)

    import tether.api.external_sessions as external_sessions
    from tether.models import ExternalRunnerType

    class Message(SimpleNamespace):
        pass

    detail = SimpleNamespace(
        first_prompt="Test prompt",
        messages=[
            Message(role="user", content="hello", thinking=None, timestamp="2026-04-10T10:00:00Z"),
            Message(role="assistant", content="world", thinking=None, timestamp="2026-04-10T10:00:01Z"),
        ],
    )
    runner_types: list[ExternalRunnerType] = []

    def _fake_get_external_session_detail(**kwargs):
        runner_type = kwargs["runner_type"]
        runner_types.append(runner_type)
        return detail if runner_type == ExternalRunnerType.PI else None

    monkeypatch.setattr(
        external_sessions,
        "get_external_session_detail",
        _fake_get_external_session_detail,
    )
    monkeypatch.setattr(
        external_sessions,
        "_get_pi_metadata",
        lambda external_id: {"model": "gpt-5.4"},
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    first = await api_client.post(
        "/api/sessions/attach",
        json={
            "external_id": "ext_pi_123",
            "runner_type": "pi",
            "directory": str(repo_dir),
        },
    )
    assert first.status_code == 201

    second = await api_client.post(
        "/api/sessions/attach",
        json={
            "external_id": "ext_pi_123",
            "runner_type": "claude_code",
            "directory": str(repo_dir),
            "platform": "legacy",
        },
    )

    assert second.status_code == 201
    assert runner_types == [ExternalRunnerType.PI, ExternalRunnerType.PI]
    assert len(bridge.outputs) == 1
    assert "Recent history" in bridge.outputs[0]["text"]


@pytest.mark.anyio
async def test_force_sync_replays_recent_history_even_with_existing_baseline(
    api_client: httpx.AsyncClient,
    monkeypatch,
    tmp_path,
) -> None:
    """Force sync should replay recent history instead of only new messages."""
    import tether.api.external_sessions as external_sessions

    class Message(SimpleNamespace):
        pass

    detail = SimpleNamespace(
        first_prompt="Test prompt",
        messages=[
            Message(role="user", content="hello", thinking=None, timestamp="2026-04-10T10:00:00Z"),
            Message(role="assistant", content="world", thinking=None, timestamp="2026-04-10T10:00:01Z"),
            Message(role="user", content="again", thinking=None, timestamp="2026-04-10T10:00:02Z"),
        ],
    )
    monkeypatch.setattr(
        external_sessions,
        "get_external_session_detail",
        lambda **kwargs: detail,
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    attach = await api_client.post(
        "/api/sessions/attach",
        json={
            "external_id": "ext_force_123",
            "runner_type": "pi",
            "directory": str(repo_dir),
        },
    )
    assert attach.status_code == 201
    session_id = attach.json()["id"]

    sync = await api_client.post(f"/api/sessions/{session_id}/sync?force=true")
    assert sync.status_code == 200
    body = sync.json()
    assert body["synced"] == 3
    assert body["total"] == 3


@pytest.mark.anyio
async def test_start_runner_error_moves_session_to_error_state(
    api_client: httpx.AsyncClient,
    fresh_store,
    monkeypatch,
    tmp_path,
) -> None:
    """If runner.start fails, session must end in ERROR instead of lingering RUNNING."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    create_response = await api_client.post(
        "/api/sessions",
        json={"directory": str(repo_dir)},
    )
    assert create_response.status_code == 201
    session_id = create_response.json()["id"]

    mock_runner = SimpleNamespace(
        runner_type="test-runner",
        start=AsyncMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr("tether.api.sessions.get_api_runner", lambda adapter: mock_runner)

    start_response = await api_client.post(
        f"/api/sessions/{session_id}/start",
        json={"prompt": "hello"},
    )

    assert start_response.status_code == 500
    body = start_response.json()
    assert body["error"]["code"] == "RUNNER_ERROR"

    session = fresh_store.get_session(session_id)
    assert session is not None
    assert session.state == SessionState.ERROR
