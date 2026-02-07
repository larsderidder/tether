"""API tests for attaching to Codex external sessions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import httpx

from tether.models import SessionState
from tether.store import SessionStore


def _write_rollout(path: Path, session_id: str) -> None:
    records = [
        {
            "timestamp": "2026-02-06T20:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": "/home/lars/xithing/tether",
            },
        },
        {
            "timestamp": "2026-02-06T20:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Hello Codex"},
                ],
            },
        },
        {
            "timestamp": "2026-02-06T20:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Hi there"},
                ],
            },
        },
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


@pytest.mark.anyio
async def test_attach_codex_session(
    api_client: httpx.AsyncClient,
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    session_id = "019b2182-8e89-77a1-a675-72857fca4fb1"
    rollout_path = codex_home / "sessions" / "2026" / "02" / "06" / f"rollout-2026-02-06T20-00-00-{session_id}.jsonl"
    _write_rollout(rollout_path, session_id)

    workdir = tmp_path / "repo"
    workdir.mkdir()

    import tether.api.external_sessions as external_sessions
    monkeypatch.setattr(external_sessions, "store", fresh_store)

    response = await api_client.post(
        "/api/sessions/attach",
        json={
            "external_id": session_id,
            "runner_type": "codex",
            "directory": str(workdir),
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["state"] == SessionState.AWAITING_INPUT.value
    assert payload["runner_type"] == "codex"
    assert payload["adapter"] == "codex_sdk_sidecar"
    assert payload["runner_session_id"] == session_id

    session = fresh_store.get_session(payload["id"])
    assert session is not None
    assert session.runner_type == "codex"
    assert session.adapter == "codex_sdk_sidecar"

    events = fresh_store.read_event_log(session.id, since_seq=0)
    event_types = [event["type"] for event in events]
    assert "session_state" in event_types
    assert "output" in event_types


@pytest.mark.anyio
async def test_sync_after_restart_does_not_duplicate(
    api_client: httpx.AsyncClient,
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Sync after agent restart (lost synced_count) must not re-emit history."""
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    session_id = "019b2182-8e89-77a1-a675-72857fca4fb1"
    rollout_path = (
        codex_home / "sessions" / "2026" / "02" / "06"
        / f"rollout-2026-02-06T20-00-00-{session_id}.jsonl"
    )
    _write_rollout(rollout_path, session_id)

    workdir = tmp_path / "repo"
    workdir.mkdir()

    import tether.api.external_sessions as external_sessions
    monkeypatch.setattr(external_sessions, "store", fresh_store)

    # Step 1: Attach — emits history messages
    attach_resp = await api_client.post(
        "/api/sessions/attach",
        json={
            "external_id": session_id,
            "runner_type": "codex",
            "directory": str(workdir),
        },
    )
    assert attach_resp.status_code == 201
    tether_session_id = attach_resp.json()["id"]

    events_after_attach = fresh_store.read_event_log(tether_session_id, since_seq=0)
    count_after_attach = len(events_after_attach)

    # Step 2: Simulate agent restart — clear the in-memory synced count
    # (synced_message_count is runtime-only, lost on restart)
    runtime = fresh_store._runtime.get(tether_session_id)
    assert runtime is not None
    runtime.synced_message_count = 0
    runtime.synced_turn_count = 0

    # Step 3: Sync — should NOT re-emit the same messages
    sync_resp = await api_client.post(
        f"/api/sessions/{tether_session_id}/sync",
    )
    assert sync_resp.status_code == 200
    sync_data = sync_resp.json()
    assert sync_data["synced"] == 0  # No new messages emitted

    events_after_sync = fresh_store.read_event_log(tether_session_id, since_seq=0)
    assert len(events_after_sync) == count_after_attach  # No duplicates added
