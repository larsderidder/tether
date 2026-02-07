import pytest
import httpx

from tether.models import SessionState


@pytest.mark.anyio
async def test_send_input_returns_503_when_sidecar_unavailable(api_client, fresh_store, tmp_path, monkeypatch) -> None:
    # Create session with a real directory.
    d = tmp_path / "repo"
    d.mkdir()
    create = await api_client.post("/api/sessions", json={"directory": str(d)})
    session_id = create.json()["id"]

    # Move it to RUNNING so /input is allowed.
    sess = fresh_store.get_session(session_id)
    sess.state = SessionState.RUNNING
    sess.adapter = "codex_sdk_sidecar"
    fresh_store.update_session(sess)

    from tether.runner.base import RunnerUnavailableError

    class FakeRunner:
        runner_type = "codex"

        async def send_input(self, _sid: str, _text: str) -> None:
            raise RunnerUnavailableError("Agent backend is not reachable")

    import tether.api.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "get_api_runner", lambda _adapter: FakeRunner())

    resp = await api_client.post(f"/api/sessions/{session_id}/input", json={"text": "hi"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "AGENT_UNAVAILABLE"
    assert "not reachable" in body["error"]["message"].lower()
