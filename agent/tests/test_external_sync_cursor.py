"""Absolute transcript cursors must not depend on a display window."""

from pathlib import Path

import pytest
from agent_sessions import RunnerType, SessionDetail, SessionMessage
from alembic import command
from alembic.config import Config

from tether.api.runner_events import ApiRunnerEvents
from tether.db import get_db_url
from tether.external_sync import sync_external_session_delta
from tether.models import SessionState
from tether.runner.pi_rpc import PiRpcRunner
from tether.store import SessionStore


def transcript_detail(messages: list[SessionMessage], limit: int) -> SessionDetail:
    """Match providers that return only the tail requested by the caller."""
    visible = messages[-limit:]
    return SessionDetail(
        id="external-long",
        runner_type=RunnerType.PI,
        directory="/tmp/repo",
        first_prompt="hello",
        last_prompt="hello",
        last_activity="2026-01-01T00:00:00Z",
        message_count=len(visible),
        is_running=False,
        messages=visible,
    )


@pytest.fixture
def attached(fresh_store: SessionStore, tmp_path: Path, monkeypatch):
    """Isolate an attached Pi session and its store references."""
    monkeypatch.setattr("tether.runner.pi_rpc.store", fresh_store)
    session = fresh_store.create_session(str(tmp_path), None)
    session.state = SessionState.AWAITING_INPUT
    session.adapter = "pi_rpc"
    session.runner_type = "pi"
    session.directory = str(tmp_path)
    fresh_store.update_session(session)
    fresh_store.set_runner_session_id(session.id, "external-long")
    return session


@pytest.mark.anyio
@pytest.mark.parametrize("baseline", [100, 500, 750])
async def test_sync_imports_only_new_messages_after_large_baseline(
    attached, fresh_store, monkeypatch, baseline: int
) -> None:
    """An absolute index must be applied to the complete transcript."""
    messages = [
        SessionMessage(role="assistant", content=f"answer-{index}")
        for index in range(baseline + 2)
    ]
    monkeypatch.setattr(
        "tether.external_sync.get_external_session_detail",
        lambda session_id, runner_type, limit: transcript_detail(messages, limit),
    )
    fresh_store.set_synced_message_count(attached.id, baseline, 0)

    first = await sync_external_session_delta(attached.id)
    second = await sync_external_session_delta(attached.id)
    imported = [
        event["data"]["text"]
        for event in fresh_store.read_event_log(attached.id)
        if event["type"] == "output"
    ]
    assert imported == [f"answer-{baseline}", f"answer-{baseline + 1}"]
    assert first.synced == 2
    assert second.synced == 0
    assert fresh_store.get_synced_message_count(attached.id) == len(messages)


@pytest.mark.anyio
async def test_live_pi_cursor_uses_full_transcript_count(
    attached, fresh_store, monkeypatch
) -> None:
    """Live RPC and history polling share the same absolute cursor."""
    messages = [
        SessionMessage(role="assistant", content=f"answer-{index}")
        for index in range(750)
    ]
    monkeypatch.setattr(
        "tether.runner.pi_rpc.get_pi_session_detail",
        lambda session_id, limit: transcript_detail(messages, limit),
    )
    await PiRpcRunner(ApiRunnerEvents())._advance_external_sync_cursor(attached.id)
    assert fresh_store.get_synced_message_count(attached.id) == 750


@pytest.mark.anyio
async def test_attach_keeps_display_history_bounded_but_cursor_absolute(
    api_client, fresh_store, tmp_path, monkeypatch
) -> None:
    """Attaching a large session must not replay its older prefix on the next sync."""
    messages = [
        SessionMessage(role="assistant", content=f"answer-{index}")
        for index in range(750)
    ]
    monkeypatch.setattr(
        "tether.api.external_sessions.get_external_session_detail",
        lambda session_id, runner_type, limit: transcript_detail(messages, limit),
    )
    response = await api_client.post(
        "/api/sessions/attach",
        json={
            "external_id": "external-long",
            "runner_type": "pi",
            "directory": str(tmp_path),
        },
    )
    assert response.status_code == 201
    session_id = response.json()["id"]
    assert fresh_store.get_synced_message_count(session_id) == 750
    imported = [
        event
        for event in fresh_store.read_event_log(session_id)
        if event["type"] == "output"
    ]
    assert len(imported) == 100


@pytest.mark.anyio
async def test_legacy_window_cursor_is_rebased_once(
    attached, fresh_store, monkeypatch
) -> None:
    """Upgrading an ambiguous legacy count must not replay an old prefix."""
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", get_db_url())
    command.downgrade(config, "9d5e7f1a2b30")
    fresh_store.set_synced_message_count(attached.id, 500, 0)
    command.upgrade(config, "head")
    restarted = SessionStore()
    assert restarted.get_synced_message_count(attached.id) == -1
    monkeypatch.setattr("tether.external_sync.store", restarted)
    messages = [
        SessionMessage(role="assistant", content=f"answer-{index}")
        for index in range(750)
    ]
    monkeypatch.setattr(
        "tether.external_sync.get_external_session_detail",
        lambda session_id, runner_type, limit: transcript_detail(messages, limit),
    )

    result = await sync_external_session_delta(attached.id)
    assert result.synced == 0
    assert restarted.get_synced_message_count(attached.id) == 750
    messages.append(SessionMessage(role="assistant", content="new answer"))
    result = await sync_external_session_delta(attached.id)
    assert result.synced == 1
    command.upgrade(config, "head")
    assert SessionStore().get_synced_message_count(attached.id) == 751


@pytest.mark.anyio
async def test_sync_cursor_advances_only_after_history_delivery_is_durable(
    attached, fresh_store, monkeypatch
) -> None:
    """If bridge send fails, the cursor may advance only with an outbox row saved."""
    from tether.bridges.manager import bridge_manager

    class FailingBridge:
        async def on_output(self, session_id: str, text: str, metadata=None) -> None:
            raise RuntimeError("bridge unavailable")

    attached.platform = "fake"
    fresh_store.update_session(attached)
    bridge_manager.register_bridge("fake", FailingBridge())
    messages = [SessionMessage(role="assistant", content="history answer")]
    monkeypatch.setattr(
        "tether.external_sync.get_external_session_detail",
        lambda session_id, runner_type, limit: transcript_detail(messages, limit),
    )

    result = await sync_external_session_delta(attached.id)
    pending = fresh_store.pending_bridge_deliveries(attached.id, "fake")

    assert result.synced == 1
    assert fresh_store.get_synced_message_count(attached.id) == 1
    assert len(pending) == 1
    assert pending[0].delivery_key.endswith("history:0:content")


@pytest.mark.anyio
async def test_interrupted_history_sync_recovers_final_without_replaying_prefix(
    attached, fresh_store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History event counts are not a source-message checkpoint after a crash."""

    class SimulatedCrash(BaseException):
        pass

    class HistoryBridge:
        def __init__(self):
            self.texts = []

        async def on_output(self, session_id, text, metadata=None):
            self.texts.append(text)

    attached.platform = "fake"
    attached.bridge_verbosity = "high"
    fresh_store.update_session(attached)
    bridge = HistoryBridge()
    monkeypatch.setattr("tether.external_sync.get_bound_bridge", lambda session: bridge)
    messages = [
        SessionMessage(role="user", content="prompt"),
        SessionMessage(role="assistant", content="partial", thinking="reason"),
        SessionMessage(role="assistant", content="final"),
    ]
    monkeypatch.setattr(
        "tether.external_sync.get_external_session_detail",
        lambda session_id, runner_type, limit: transcript_detail(messages, limit),
    )
    original_enqueue = fresh_store.enqueue_bridge_delivery

    def crash_before_final(**kwargs):
        if kwargs["payload"].get("text") == "final":
            raise SimulatedCrash
        return original_enqueue(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fresh_store, "enqueue_bridge_delivery", crash_before_final)
        with pytest.raises(SimulatedCrash):
            await sync_external_session_delta(attached.id)

    restarted = SessionStore()
    monkeypatch.setattr("tether.external_sync.store", restarted)
    monkeypatch.setattr("tether.api.emit.store", restarted)
    await sync_external_session_delta(attached.id)
    assert bridge.texts.count("final") == 1
    assert bridge.texts.count("👤 User: prompt") == 1
    assert restarted.get_synced_message_count(attached.id) == len(messages)


@pytest.mark.anyio
async def test_unavailable_configured_bridge_still_gets_durable_history(
    attached, fresh_store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed bridge startup must not discard imported delivery work."""
    attached.platform = "missing"
    fresh_store.update_session(attached)
    monkeypatch.setattr("tether.external_sync.get_bound_bridge", lambda session: None)
    messages = [SessionMessage(role="assistant", content="pending answer")]
    monkeypatch.setattr(
        "tether.external_sync.get_external_session_detail",
        lambda session_id, runner_type, limit: transcript_detail(messages, limit),
    )
    await sync_external_session_delta(attached.id)
    pending = fresh_store.pending_bridge_deliveries(attached.id, "missing")
    assert len(pending) == 1
    assert "pending answer" in pending[0].payload


@pytest.mark.anyio
async def test_slow_history_send_renews_its_outbox_claim(
    attached, fresh_store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History and live sends must keep the same lease during platform I/O."""
    import asyncio
    from tether.external_sync import _drain_history_outbox

    attached.platform = "fake"
    fresh_store.update_session(attached)
    fresh_store.enqueue_bridge_delivery(
        session_id=attached.id,
        platform="fake",
        delivery_key="slow",
        method="on_output",
        payload={"text": "slow answer"},
    )
    renewed = asyncio.Event()
    original_extend = fresh_store.extend_bridge_delivery_claim

    def extend(delivery_id):
        original_extend(delivery_id)
        renewed.set()

    class SlowBridge:
        async def on_output(self, session_id, text, metadata=None):
            await asyncio.wait_for(renewed.wait(), timeout=1)

    monkeypatch.setattr(fresh_store, "extend_bridge_delivery_claim", extend)
    monkeypatch.setattr("tether.bridges.retry._BRIDGE_CLAIM_RENEW_INTERVAL_S", 0.01)
    assert await _drain_history_outbox(attached, SlowBridge()) is True
    assert renewed.is_set()


@pytest.mark.anyio
async def test_identical_history_without_source_id_is_not_silently_deduplicated(
    attached, fresh_store: SessionStore
) -> None:
    """Identical text is not proof that two imported events have the same identity."""
    from tether.external_sync import send_history_to_bridge

    class HistoryBridge:
        def __init__(self):
            self.texts = []

        async def on_output(self, session_id, text, metadata=None):
            self.texts.append(text)

    attached.platform = "fake"
    fresh_store.update_session(attached)
    bridge = HistoryBridge()
    for _ in range(2):
        await send_history_to_bridge(
            session=attached,
            bridge=bridge,
            text="repeat",
            metadata={"is_history": True, "role": "user"},
        )
    assert bridge.texts == ["repeat", "repeat"]
