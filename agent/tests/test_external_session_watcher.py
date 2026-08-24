"""Tests for external session sync services."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_sessions import RunnerType, SessionDetail, SessionMessage

from tether.api.schemas import SyncResult
from tether.external_session_watcher import ExternalSessionWatcher
from tether.models import SessionState
from tether.store import SessionStore


class MockBridge:
    """Minimal bridge that records imported history posts."""

    def __init__(self) -> None:
        """Create an empty bridge recorder."""
        self.output_calls: list[dict] = []

    async def on_output(
        self, session_id: str, text: str, metadata: dict | None = None
    ) -> None:
        """Record an output relay."""
        self.output_calls.append(
            {"session_id": session_id, "text": text, "metadata": metadata}
        )


def _detail(
    external_id: str,
    messages: list[SessionMessage],
    *,
    runner_type: RunnerType = RunnerType.CODEX,
) -> SessionDetail:
    """Build an external session detail fixture."""
    return SessionDetail(
        id=external_id,
        runner_type=runner_type,
        directory="/tmp/repo",
        first_prompt=messages[0].content if messages else None,
        last_prompt=None,
        last_activity="2026-07-01T12:00:00Z",
        message_count=len(messages),
        is_running=False,
        messages=messages,
    )


def _attached_session(
    fresh_store: SessionStore, tmp_path: Path, *, platform: str | None = "mock"
):
    """Create a Tether session attached to an external session."""
    workdir = tmp_path / "repo"
    workdir.mkdir(exist_ok=True)
    session = fresh_store.create_session(str(workdir), None)
    session.state = SessionState.AWAITING_INPUT
    session.runner_type = "codex"
    session.adapter = "codex_sdk_sidecar"
    session.directory = str(workdir)
    session.platform = platform
    fresh_store.update_session(session)
    fresh_store.set_runner_session_id(session.id, "external-1")
    return session


@pytest.mark.anyio
async def test_sync_external_session_delta_replays_only_new_messages(
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The shared sync service preserves the API delta behavior."""
    import tether.external_sync as external_sync
    from tether.bridges.manager import bridge_manager

    session = _attached_session(fresh_store, tmp_path)
    fresh_store.set_synced_message_count(session.id, 1, 1)
    bridge = MockBridge()
    bridge_manager.register_bridge("mock", bridge)

    messages = [
        SessionMessage(role="user", content="Hello"),
        SessionMessage(role="assistant", content="Hi there"),
    ]
    monkeypatch.setattr(
        external_sync,
        "get_external_session_detail",
        lambda **_: _detail("external-1", messages),
    )

    result = await external_sync.sync_external_session_delta(session.id, source="test")

    assert result == SyncResult(synced=1, total=2)
    assert [call["text"] for call in bridge.output_calls] == ["Hi there"]
    assert fresh_store.get_synced_message_count(session.id) == 2


@pytest.mark.anyio
async def test_sync_initializes_migrated_cursor_without_replay(
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Existing sessions establish a durable baseline without duplicate messages."""
    import tether.external_sync as external_sync
    from tether.bridges.manager import bridge_manager

    session = _attached_session(fresh_store, tmp_path)
    session.synced_message_count = None
    session.synced_turn_count = None
    fresh_store.update_session(session)
    bridge = MockBridge()
    bridge_manager.register_bridge("mock", bridge)

    messages = [
        SessionMessage(role="user", content="Hello"),
        SessionMessage(role="assistant", content="Already delivered"),
    ]
    monkeypatch.setattr(
        external_sync,
        "get_external_session_detail",
        lambda **_: _detail("external-1", messages),
    )

    result = await external_sync.sync_external_session_delta(session.id, source="test")

    assert result == SyncResult(synced=0, total=2)
    assert bridge.output_calls == []
    assert fresh_store.get_synced_message_count(session.id) == 2
    assert fresh_store.get_synced_turn_count(session.id) == 1


@pytest.mark.anyio
async def test_sync_flushes_buffered_tui_activity_before_final(
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """External TUI activity is buffered, but final output is immediate."""
    import tether.external_sync as external_sync
    from tether.bridges.manager import bridge_manager

    session = _attached_session(fresh_store, tmp_path)
    session.bridge_buffer_max_seconds = 30.0
    fresh_store.update_session(session)
    fresh_store.set_synced_message_count(session.id, 1, 1)
    bridge = MockBridge()
    bridge_manager.register_bridge("mock", bridge)

    messages = [
        SessionMessage(role="user", content="Prompt"),
        SessionMessage(role="assistant", content="Working..."),
        SessionMessage(role="assistant", content="Done"),
    ]
    monkeypatch.setattr(
        external_sync,
        "get_external_session_detail",
        lambda **_: _detail("external-1", messages),
    )

    result = await external_sync.sync_external_session_delta(session.id, source="test")

    assert result == SyncResult(synced=2, total=3)
    assert [call["text"] for call in bridge.output_calls] == ["Working...", "Done"]
    assert bridge.output_calls[-1]["metadata"]["final"] is True


@pytest.mark.anyio
async def test_sync_uses_session_buffer_delay_for_tui_activity(
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """External TUI activity uses the session buffer override."""
    import tether.external_sync as external_sync
    from tether.bridges.manager import bridge_manager

    session = _attached_session(fresh_store, tmp_path)
    session.bridge_buffer_max_seconds = 0.0
    fresh_store.update_session(session)
    fresh_store.set_synced_message_count(session.id, 1, 1)
    bridge = MockBridge()
    bridge_manager.register_bridge("mock", bridge)

    messages = [
        SessionMessage(role="user", content="Prompt"),
        SessionMessage(role="assistant", content="Working..."),
        SessionMessage(role="user", content="Next prompt"),
    ]
    monkeypatch.setattr(
        external_sync,
        "get_external_session_detail",
        lambda **_: _detail("external-1", messages),
    )

    result = await external_sync.sync_external_session_delta(session.id, source="test")

    assert result == SyncResult(synced=2, total=3)
    assert [call["text"] for call in bridge.output_calls] == [
        "Working...",
        "👤 User: Next prompt",
    ]


@pytest.mark.anyio
async def test_sync_respects_none_verbosity_for_tui_activity(
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verbosity none suppresses imported non-final assistant activity."""
    import tether.external_sync as external_sync
    from tether.bridges.manager import bridge_manager

    session = _attached_session(fresh_store, tmp_path)
    session.bridge_verbosity = "none"
    session.bridge_buffer_max_seconds = 0.0
    fresh_store.update_session(session)
    fresh_store.set_synced_message_count(session.id, 1, 1)
    bridge = MockBridge()
    bridge_manager.register_bridge("mock", bridge)

    messages = [
        SessionMessage(role="user", content="Prompt"),
        SessionMessage(role="assistant", content="Working..."),
        SessionMessage(role="assistant", content="Done"),
    ]
    monkeypatch.setattr(
        external_sync,
        "get_external_session_detail",
        lambda **_: _detail("external-1", messages),
    )

    result = await external_sync.sync_external_session_delta(session.id, source="test")

    assert result == SyncResult(synced=2, total=3)
    assert [call["text"] for call in bridge.output_calls] == ["Done"]
    assert bridge.output_calls[-1]["metadata"]["final"] is True


@pytest.mark.anyio
async def test_sync_replays_external_thinking_when_verbosity_allows_it(
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Imported assistant thinking is bridged as non-final activity."""
    import tether.external_sync as external_sync
    from tether.bridges.manager import bridge_manager

    session = _attached_session(fresh_store, tmp_path)
    session.bridge_verbosity = "minimal"
    fresh_store.update_session(session)
    fresh_store.set_synced_message_count(session.id, 1, 1)
    bridge = MockBridge()
    bridge_manager.register_bridge("mock", bridge)

    messages = [
        SessionMessage(role="user", content="Prompt"),
        SessionMessage(role="assistant", content="Answer", thinking="Reasoning"),
    ]
    monkeypatch.setattr(
        external_sync,
        "get_external_session_detail",
        lambda **_: _detail("external-1", messages),
    )

    result = await external_sync.sync_external_session_delta(session.id, source="test")

    assert result == SyncResult(synced=1, total=2)
    assert [call["text"] for call in bridge.output_calls] == ["Reasoning", "Answer"]


@pytest.mark.anyio
async def test_sync_suppresses_external_thinking_when_verbosity_none(
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verbosity none suppresses imported thinking but keeps final output."""
    import tether.external_sync as external_sync
    from tether.bridges.manager import bridge_manager

    session = _attached_session(fresh_store, tmp_path)
    session.bridge_verbosity = "none"
    fresh_store.update_session(session)
    fresh_store.set_synced_message_count(session.id, 1, 1)
    bridge = MockBridge()
    bridge_manager.register_bridge("mock", bridge)

    messages = [
        SessionMessage(role="user", content="Prompt"),
        SessionMessage(role="assistant", content="Answer", thinking="Reasoning"),
    ]
    monkeypatch.setattr(
        external_sync,
        "get_external_session_detail",
        lambda **_: _detail("external-1", messages),
    )

    result = await external_sync.sync_external_session_delta(session.id, source="test")

    assert result == SyncResult(synced=1, total=2)
    assert [call["text"] for call in bridge.output_calls] == ["Answer"]


@pytest.mark.anyio
async def test_sync_recovers_only_messages_after_live_event_log(
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Managed pi sessions recover missed messages without replaying old output."""
    import tether.external_sync as external_sync
    from tether.bridges.manager import bridge_manager

    session = _attached_session(fresh_store, tmp_path)
    session.runner_type = "pi"
    session.adapter = "pi_rpc"
    fresh_store.update_session(session)
    bridge = MockBridge()
    bridge_manager.register_bridge("mock", bridge)
    await fresh_store.emit(
        session.id,
        {
            "session_id": session.id,
            "ts": "2026-07-15T04:38:59Z",
            "seq": fresh_store.next_seq(session.id),
            "type": "output",
            "data": {"text": "already shown", "final": False},
        },
    )

    messages = [
        SessionMessage(
            role="user",
            content="Prompt",
            timestamp="2026-07-15T04:30:00Z",
        ),
        SessionMessage(
            role="assistant",
            content="Old answer",
            timestamp="2026-07-15T04:38:00Z",
        ),
        SessionMessage(
            role="assistant",
            content="Recovered final answer",
            timestamp="2026-07-15T04:39:01Z",
        ),
    ]
    monkeypatch.setattr(
        external_sync,
        "get_external_session_detail",
        lambda **_: _detail("external-1", messages, runner_type=RunnerType.PI),
    )

    result = await external_sync.sync_external_session_delta(session.id, source="test")

    assert result == SyncResult(synced=1, total=3)
    assert [call["text"] for call in bridge.output_calls] == ["Recovered final answer"]
    assert fresh_store.get_synced_message_count(session.id) == 3


@pytest.mark.anyio
async def test_sync_skips_live_user_input_echoes(
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Watcher history sync does not echo prompts already sent through Tether."""
    import tether.external_sync as external_sync
    from tether.bridges.manager import bridge_manager

    session = _attached_session(fresh_store, tmp_path)
    session.runner_type = "pi"
    session.adapter = "pi_rpc"
    fresh_store.update_session(session)
    fresh_store.set_synced_message_count(session.id, 1, 1)
    bridge = MockBridge()
    bridge_manager.register_bridge("mock", bridge)
    await fresh_store.emit(
        session.id,
        {
            "session_id": session.id,
            "ts": "2026-07-19T06:54:50Z",
            "seq": fresh_store.next_seq(session.id),
            "type": "user_input",
            "data": {"text": "You didn’t find svarogsden, why"},
        },
    )

    messages = [
        SessionMessage(role="assistant", content="old"),
        SessionMessage(
            role="user",
            content="You didn’t find svarogsden, why",
            timestamp="2026-07-19T06:54:50.618Z",
        ),
    ]
    monkeypatch.setattr(
        external_sync,
        "get_external_session_detail",
        lambda **_: _detail("external-1", messages, runner_type=RunnerType.PI),
    )

    result = await external_sync.sync_external_session_delta(session.id, source="test")

    assert result == SyncResult(synced=1, total=2)
    assert bridge.output_calls == []
    history_user_events = [
        event
        for event in fresh_store.read_event_log(session.id, since_seq=0)
        if event.get("type") == "user_input"
        and (event.get("data") or {}).get("is_history")
    ]
    assert history_user_events == []
    assert fresh_store.get_synced_message_count(session.id) == 2


@pytest.mark.anyio
async def test_sync_matches_live_user_inputs_by_occurrence(
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """One live prompt suppresses only one matching history message."""
    import tether.external_sync as external_sync

    session = _attached_session(fresh_store, tmp_path)
    fresh_store.set_synced_message_count(session.id, 1, 1)
    await fresh_store.emit(
        session.id,
        {
            "session_id": session.id,
            "ts": "2026-07-19T06:54:50Z",
            "seq": fresh_store.next_seq(session.id),
            "type": "user_input",
            "data": {"text": "continue"},
        },
    )
    messages = [
        SessionMessage(role="assistant", content="old"),
        SessionMessage(role="user", content="continue"),
        SessionMessage(role="user", content="continue"),
    ]
    monkeypatch.setattr(
        external_sync,
        "get_external_session_detail",
        lambda **_: _detail("external-1", messages, runner_type=RunnerType.PI),
    )

    result = await external_sync.sync_external_session_delta(session.id, source="test")

    assert result == SyncResult(synced=2, total=3)
    history_user_events = [
        event
        for event in fresh_store.read_event_log(session.id, since_seq=0)
        if event.get("type") == "user_input"
        and (event.get("data") or {}).get("is_history")
    ]
    assert [event["data"]["text"] for event in history_user_events] == ["continue"]


@pytest.mark.anyio
async def test_watcher_initial_sync_uses_recent_lookback_only(
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Watcher baseline recovery imports only recent messages."""
    import tether.external_sync as external_sync
    from tether.bridges.manager import bridge_manager

    session = _attached_session(fresh_store, tmp_path)
    bridge = MockBridge()
    bridge_manager.register_bridge("mock", bridge)

    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=2)).isoformat()
    recent = (now - timedelta(minutes=10)).isoformat()
    messages = [
        SessionMessage(role="user", content="Old prompt", timestamp=old),
        SessionMessage(role="assistant", content="Old answer", timestamp=old),
        SessionMessage(role="user", content="Recent prompt", timestamp=recent),
        SessionMessage(role="assistant", content="Recent answer", timestamp=recent),
    ]
    monkeypatch.setattr(
        external_sync,
        "get_external_session_detail",
        lambda **_: _detail("external-1", messages),
    )

    result = await external_sync.sync_external_session_delta(
        session.id,
        source="watcher",
        initial_lookback_seconds=3600,
    )

    assert result == SyncResult(synced=2, total=4)
    assert [call["text"] for call in bridge.output_calls] == [
        "👤 User: Recent prompt",
        "Recent answer",
    ]
    assert fresh_store.get_synced_message_count(session.id) == 4


@pytest.mark.anyio
async def test_watcher_initial_sync_advances_cursor_when_no_recent_messages(
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Watcher baseline recovery skips old history and avoids retry loops."""
    import tether.external_sync as external_sync
    from tether.bridges.manager import bridge_manager

    session = _attached_session(fresh_store, tmp_path)
    bridge = MockBridge()
    bridge_manager.register_bridge("mock", bridge)

    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    messages = [
        SessionMessage(role="user", content="Old prompt", timestamp=old),
        SessionMessage(role="assistant", content="Old answer", timestamp=old),
    ]
    monkeypatch.setattr(
        external_sync,
        "get_external_session_detail",
        lambda **_: _detail("external-1", messages),
    )

    result = await external_sync.sync_external_session_delta(
        session.id,
        source="watcher",
        initial_lookback_seconds=3600,
    )

    assert result == SyncResult(synced=0, total=2)
    assert bridge.output_calls == []
    assert fresh_store.get_synced_message_count(session.id) == 2


@pytest.mark.anyio
async def test_watcher_syncs_platform_bound_external_sessions(
    fresh_store: SessionStore,
    tmp_path: Path,
) -> None:
    """The watcher syncs sessions that have both external ids and platforms."""
    session = _attached_session(fresh_store, tmp_path)
    synced: list[str] = []

    async def sync_func(session_id: str) -> SyncResult:
        """Record watcher sync calls."""
        synced.append(session_id)
        return SyncResult(synced=0, total=0)

    watcher = ExternalSessionWatcher(sync_func=sync_func)
    watcher.register(session.id)

    await watcher.sync_once()

    assert synced == [session.id]


@pytest.mark.anyio
async def test_watcher_skips_sessions_without_runner_session_id(
    fresh_store: SessionStore,
    tmp_path: Path,
) -> None:
    """The watcher ignores sessions that are not attached externally."""
    session = fresh_store.create_session(str(tmp_path / "repo"), None)
    session.platform = "mock"
    fresh_store.update_session(session)
    synced: list[str] = []

    async def sync_func(session_id: str) -> SyncResult:
        """Record unexpected watcher sync calls."""
        synced.append(session_id)
        return SyncResult(synced=0, total=0)

    watcher = ExternalSessionWatcher(sync_func=sync_func)
    watcher.register(session.id)

    await watcher.sync_once()

    assert synced == []


@pytest.mark.anyio
async def test_watcher_skips_unbound_sessions(
    fresh_store: SessionStore,
    tmp_path: Path,
) -> None:
    """The first watcher version only syncs bridge-bound sessions."""
    session = _attached_session(fresh_store, tmp_path, platform=None)
    synced: list[str] = []

    async def sync_func(session_id: str) -> SyncResult:
        """Record unexpected watcher sync calls."""
        synced.append(session_id)
        return SyncResult(synced=0, total=0)

    watcher = ExternalSessionWatcher(sync_func=sync_func)
    watcher.register(session.id)

    await watcher.sync_once()

    assert synced == []


@pytest.mark.anyio
async def test_watcher_skips_managed_runner_sessions(
    fresh_store: SessionStore,
    tmp_path: Path,
) -> None:
    """The watcher does not mirror Tether-owned runner sessions."""
    session = _attached_session(fresh_store, tmp_path)
    session.started_at = "2026-07-01T12:00:00Z"
    fresh_store.update_session(session)
    synced: list[str] = []

    async def sync_func(session_id: str) -> SyncResult:
        """Record unexpected watcher sync calls."""
        synced.append(session_id)
        return SyncResult(synced=0, total=0)

    watcher = ExternalSessionWatcher(sync_func=sync_func)
    watcher.register(session.id)

    await watcher.sync_once()

    assert synced == []


@pytest.mark.anyio
async def test_watcher_syncs_idle_pi_rpc_with_live_process(
    fresh_store: SessionStore,
    tmp_path: Path,
) -> None:
    """Idle pi RPC sessions are watched even while RPC stays connected."""
    session = _attached_session(fresh_store, tmp_path)
    session.runner_type = "pi"
    session.adapter = "pi_rpc"
    fresh_store.update_session(session)
    fresh_store.set_process(session.id, MagicMock())
    synced: list[str] = []

    async def sync_func(session_id: str) -> SyncResult:
        """Record watcher sync calls."""
        synced.append(session_id)
        return SyncResult(synced=0, total=0)

    watcher = ExternalSessionWatcher(sync_func=sync_func)
    watcher.register(session.id)

    await watcher.sync_once()

    assert synced == [session.id]


@pytest.mark.anyio
async def test_watcher_skips_running_attached_pi_with_live_process(
    fresh_store: SessionStore,
    tmp_path: Path,
) -> None:
    """Running attached pi sessions wait until the turn is idle."""
    session = _attached_session(fresh_store, tmp_path)
    session.runner_type = "pi"
    session.adapter = "pi_rpc"
    session.external_agent_id = "external-1"
    session.external_agent_type = "pi"
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)
    fresh_store.set_process(session.id, MagicMock())
    synced: list[str] = []

    async def sync_func(session_id: str) -> SyncResult:
        """Record unexpected watcher sync calls."""
        synced.append(session_id)
        return SyncResult(synced=0, total=0)

    watcher = ExternalSessionWatcher(sync_func=sync_func)
    watcher.register(session.id)

    await watcher.sync_once()

    assert synced == []


@pytest.mark.anyio
async def test_watcher_errors_do_not_stop_future_syncs(
    fresh_store: SessionStore,
    tmp_path: Path,
) -> None:
    """A failed watcher sync does not prevent later passes."""
    session = _attached_session(fresh_store, tmp_path)
    attempts = 0
    synced: list[str] = []

    async def sync_func(session_id: str) -> SyncResult:
        """Fail once, then record the next sync."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        synced.append(session_id)
        return SyncResult(synced=0, total=0)

    watcher = ExternalSessionWatcher(sync_func=sync_func)
    watcher.register(session.id)

    await watcher.sync_once()
    await watcher.sync_once()

    assert attempts == 2
    assert synced == [session.id]
