"""Lifespan integration tests for startup lifecycle recovery."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import pytest

from tether.api.runner_registry import RunnerRegistry
from tether.models import SessionState
from tether.store import SessionStore


class NoopRunnerEvents:
    """RunnerEvents implementation that keeps tests local and deterministic."""

    async def on_output(
        self, session_id: str, stream: str, text: str, **kwargs
    ) -> None:
        """Ignore runner output."""

    async def on_header(self, session_id: str, **kwargs) -> None:
        """Ignore runner headers."""

    async def on_error(self, session_id: str, code: str, message: str) -> None:
        """Ignore runner errors."""

    async def on_exit(self, session_id: str, exit_code: int | None) -> None:
        """Ignore runner exits."""

    async def on_awaiting_input(self, session_id: str) -> None:
        """Ignore awaiting-input callbacks."""

    async def on_metadata(
        self, session_id: str, key: str, value: object, raw: str
    ) -> None:
        """Ignore metadata."""

    async def on_heartbeat(self, session_id: str, elapsed_s: float, done: bool) -> None:
        """Ignore heartbeats."""

    async def on_permission_request(
        self,
        session_id: str,
        request_id: str,
        tool_name: str,
        tool_input: dict,
        suggestions: list | None = None,
    ) -> None:
        """Ignore permission requests."""

    async def on_permission_resolved(
        self,
        session_id: str,
        request_id: str,
        resolved_by: str,
        allowed: bool,
        message: str | None = None,
    ) -> None:
        """Ignore permission resolutions."""


class FakeProcess:
    """Minimal live process stand-in for store runtime tracking."""

    returncode: int | None = None


class FakeRunner:
    """Deterministic runner with explicit lifecycle hooks."""

    runner_type = "fake"

    def __init__(self) -> None:
        """Create a fake runner with no recoverable sessions by default."""
        self.recoverable: set[str] = set()
        self.recovered: list[str] = []
        self.shutdown_called = False
        self.disposed: list[str] = []
        self.dispose_error: Exception | None = None
        self.dispose_wait: asyncio.Event | None = None
        self.shutdown_error: BaseException | None = None

    async def recover_session(self, session_id: str) -> bool:
        """Recover only sessions listed in recoverable."""
        self.recovered.append(session_id)
        if session_id not in self.recoverable:
            return False

        import tether.store as store_module

        store_module.store.set_process(session_id, FakeProcess())
        return True

    async def dispose_session(self, session_id: str) -> bool:
        """Dispose a fake owned session or raise a configured error."""
        self.disposed.append(session_id)
        if self.dispose_wait is not None:
            await self.dispose_wait.wait()
        if self.dispose_error:
            raise self.dispose_error
        return True

    async def shutdown(self) -> None:
        """Record that registry shutdown reached the runner."""
        self.shutdown_called = True
        if self.shutdown_error:
            raise self.shutdown_error


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    """Install a fake cached automation runner in the global registry."""
    import tether.api.runner_events as runner_events

    runner = FakeRunner()
    registry = RunnerRegistry(NoopRunnerEvents())
    registry._runners["automation"] = runner
    monkeypatch.setattr(runner_events, "_registry", registry)
    return runner


def _patch_store(monkeypatch: pytest.MonkeyPatch, new_store: SessionStore) -> None:
    """Patch modules that import the store singleton."""
    import tether.api.debug
    import tether.api.emit
    import tether.api.events
    import tether.api.external_sessions
    import tether.api.git
    import tether.api.runner_events
    import tether.api.sessions
    import tether.api.state
    import tether.api.status
    import tether.external_session_watcher
    import tether.external_sync
    import tether.maintenance
    import tether.store

    modules = [
        tether.store,
        tether.api.state,
        tether.api.sessions,
        tether.api.runner_events,
        tether.api.emit,
        tether.api.debug,
        tether.api.events,
        tether.api.external_sessions,
        tether.api.git,
        tether.external_session_watcher,
        tether.external_sync,
        tether.api.status,
        tether.maintenance,
    ]
    for module in modules:
        monkeypatch.setattr(module, "store", new_store)


def _persist_session(store: SessionStore, state: SessionState) -> str:
    """Create a persisted session in the requested lifecycle state."""
    session = store.create_session("repo_test", "main")
    session.adapter = "automation"
    session.runner_type = "fake"
    session.state = state
    store.update_session(session)
    return session.id


def _reload_store(monkeypatch: pytest.MonkeyPatch) -> SessionStore:
    """Simulate a Tether restart by reloading sessions from the database."""
    new_store = SessionStore()
    _patch_store(monkeypatch, new_store)
    return new_store


@asynccontextmanager
async def _lifespan_client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    """Run the real FastAPI lifespan while replacing external services."""
    import tether.main as main

    async def dormant_loop() -> None:
        await asyncio.Event().wait()

    async def noop_async() -> None:
        return None

    had_agent_token = hasattr(main.app.state, "agent_token")
    previous_agent_token = getattr(main.app.state, "agent_token", None)

    monkeypatch.setattr(main.settings, "token", lambda: "test-token")
    monkeypatch.setattr(main, "_init_bridges", noop_async)
    monkeypatch.setattr(main, "_subscribe_existing_sessions", lambda: None)
    monkeypatch.setattr(main, "maintenance_loop", dormant_loop)
    monkeypatch.setattr(main, "telegram_topic_cleanup_loop", dormant_loop)
    monkeypatch.setattr(main.external_session_watcher, "start", noop_async)
    monkeypatch.setattr(main.external_session_watcher, "stop", noop_async)
    monkeypatch.setattr(main, "log_ui_urls", lambda port: None)

    try:
        async with main.app.router.lifespan_context(main.app):
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                yield client
    finally:
        if had_agent_token:
            main.app.state.agent_token = previous_agent_token
        elif hasattr(main.app.state, "agent_token"):
            delattr(main.app.state, "agent_token")


@pytest.mark.anyio
async def test_lifespan_recovers_owned_running_session_with_runner_hook(
    fresh_store: SessionStore,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner-owned persisted RUNNING session stays running only after recovery."""
    session_id = _persist_session(fresh_store, SessionState.RUNNING)
    fake_runner.recoverable.add(session_id)
    restarted_store = _reload_store(monkeypatch)

    async with _lifespan_client(monkeypatch) as client:
        response = await client.get("/api/health")
        assert response.status_code == 200
        recovered = restarted_store.get_session(session_id)
        assert recovered is not None
        assert recovered.state == SessionState.RUNNING
        assert restarted_store.get_process(session_id) is not None

    assert fake_runner.recovered == [session_id]
    assert fake_runner.shutdown_called is True


@pytest.mark.anyio
async def test_lifespan_marks_unrecoverable_owned_running_session_error(
    fresh_store: SessionStore,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted RUNNING session is not kept running without a live runner."""
    session_id = _persist_session(fresh_store, SessionState.RUNNING)
    restarted_store = _reload_store(monkeypatch)

    async with _lifespan_client(monkeypatch):
        recovered = restarted_store.get_session(session_id)
        assert recovered is not None
        assert recovered.state == SessionState.ERROR
        assert recovered.exit_code == 255

    assert fake_runner.recovered == [session_id]


@pytest.mark.anyio
async def test_lifespan_moves_interrupting_session_to_awaiting_input(
    fresh_store: SessionStore,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persisted INTERRUPTING sessions cannot remain stuck after restart."""
    session_id = _persist_session(fresh_store, SessionState.INTERRUPTING)
    restarted_store = _reload_store(monkeypatch)

    async with _lifespan_client(monkeypatch):
        recovered = restarted_store.get_session(session_id)
        assert recovered is not None
        assert recovered.state == SessionState.AWAITING_INPUT

    assert fake_runner.recovered == []


@pytest.mark.anyio
async def test_lifespan_keeps_live_attached_external_running_without_runner_claim(
    fresh_store: SessionStore,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live attached external sessions are not claimed by the cached runner."""
    session_id = _persist_session(fresh_store, SessionState.RUNNING)
    session = fresh_store.get_session(session_id)
    assert session is not None
    session.runner_session_id = "pi-live"
    session.external_agent_id = "pi-live"
    session.external_agent_type = "pi"
    fresh_store._persist_session(session, allow_runner_session_id_change=True)
    restarted_store = _reload_store(monkeypatch)

    import tether.maintenance as maintenance

    monkeypatch.setattr(
        maintenance,
        "is_pi_session_running",
        lambda external_id: external_id == "pi-live",
    )

    async with _lifespan_client(monkeypatch):
        recovered = restarted_store.get_session(session_id)
        assert recovered is not None
        assert recovered.state == SessionState.RUNNING

    assert fake_runner.recovered == []


@pytest.mark.anyio
async def test_lifespan_moves_dead_attached_external_session_to_awaiting_input(
    fresh_store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dead attached external sessions become input-ready instead of staying RUNNING."""
    session_id = _persist_session(fresh_store, SessionState.RUNNING)
    session = fresh_store.get_session(session_id)
    assert session is not None
    session.runner_session_id = "pi-dead"
    session.external_agent_id = "pi-dead"
    session.external_agent_type = "pi"
    fresh_store._persist_session(session, allow_runner_session_id_change=True)
    restarted_store = _reload_store(monkeypatch)

    import tether.maintenance as maintenance

    monkeypatch.setattr(maintenance, "is_pi_session_running", lambda external_id: False)

    async with _lifespan_client(monkeypatch):
        recovered = restarted_store.get_session(session_id)
        assert recovered is not None
        assert recovered.state == SessionState.AWAITING_INPUT


@pytest.mark.anyio
async def test_interrupt_recovery_uses_interrupt_age_not_last_activity(
    fresh_store: SessionStore,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh heartbeats cannot indefinitely delay stale interrupt recovery."""
    import tether.maintenance as maintenance

    _patch_store(monkeypatch, fresh_store)

    session_id = _persist_session(fresh_store, SessionState.INTERRUPTING)
    session = fresh_store.get_session(session_id)
    assert session is not None
    session.last_activity_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_store.update_session(session)
    monkeypatch.setattr(
        maintenance,
        "_interrupt_started_at",
        lambda session, now_ts: datetime.now(timezone.utc).timestamp() - 600,
    )

    await maintenance._recover_stale_interrupting_sessions(
        datetime.now(timezone.utc).timestamp(), idle_timeout_s=60
    )

    recovered = fresh_store.get_session(session_id)
    assert recovered is not None
    assert recovered.state == SessionState.AWAITING_INPUT
    assert fake_runner.disposed == [session_id]


@pytest.mark.anyio
async def test_interrupt_recovery_reports_error_when_owned_dispose_fails(
    fresh_store: SessionStore,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed owned-process disposal is not reported as input-ready."""
    import tether.maintenance as maintenance

    _patch_store(monkeypatch, fresh_store)

    session_id = _persist_session(fresh_store, SessionState.INTERRUPTING)
    monkeypatch.setattr(
        maintenance,
        "_interrupt_started_at",
        lambda session, now_ts: datetime.now(timezone.utc).timestamp() - 600,
    )
    fake_runner.dispose_error = RuntimeError("still running")

    await maintenance._recover_stale_interrupting_sessions(
        datetime.now(timezone.utc).timestamp(), idle_timeout_s=60
    )

    recovered = fresh_store.get_session(session_id)
    assert recovered is not None
    assert recovered.state == SessionState.ERROR
    assert fake_runner.disposed == [session_id]


@pytest.mark.anyio
async def test_interrupt_recovery_reports_error_when_owned_dispose_hangs(
    fresh_store: SessionStore,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hung owned-process disposal is not reported as input-ready."""
    import tether.maintenance as maintenance

    _patch_store(monkeypatch, fresh_store)
    monkeypatch.setattr(maintenance, "RUNNER_DISPOSE_TIMEOUT_SECONDS", 0.01)

    session_id = _persist_session(fresh_store, SessionState.INTERRUPTING)
    monkeypatch.setattr(
        maintenance,
        "_interrupt_started_at",
        lambda session, now_ts: datetime.now(timezone.utc).timestamp() - 600,
    )
    fake_runner.dispose_wait = asyncio.Event()

    await maintenance._recover_stale_interrupting_sessions(
        datetime.now(timezone.utc).timestamp(), idle_timeout_s=60
    )

    recovered = fresh_store.get_session(session_id)
    assert recovered is not None
    assert recovered.state == SessionState.ERROR
    assert fake_runner.disposed == [session_id]


@pytest.mark.anyio
async def test_shutdown_services_stops_bridges_when_runner_shutdown_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge flushing still runs when runner shutdown reports cancellation."""
    import tether.main as main

    class CancelledRegistry:
        async def shutdown(self) -> None:
            raise asyncio.CancelledError

    async def noop_async() -> None:
        return None

    bridge_calls: list[str] = []

    async def stop_bridges() -> None:
        bridge_calls.append("bridges")

    monkeypatch.setattr(main.external_session_watcher, "stop", noop_async)
    monkeypatch.setattr(main, "get_runner_registry", lambda: CancelledRegistry())
    monkeypatch.setattr(main, "_stop_bridges", stop_bridges)
    monkeypatch.setattr(main.settings, "opencode_sidecar_managed", lambda: False)

    with pytest.raises(RuntimeError, match="runners"):
        await main._shutdown_services()

    assert bridge_calls == ["bridges"]


@pytest.mark.anyio
async def test_registry_shutdown_continues_after_cancelled_runner() -> None:
    """One cancelled runner cleanup does not prevent other runners from shutting down."""

    class CancelledRunner:
        runner_type = "cancelled"

        async def shutdown(self) -> None:
            raise asyncio.CancelledError

    class GoodRunner:
        runner_type = "good"

        def __init__(self) -> None:
            self.shutdown_called = False

        async def shutdown(self) -> None:
            self.shutdown_called = True

    good_runner = GoodRunner()
    registry = RunnerRegistry(NoopRunnerEvents())
    registry._runners = {"cancelled": CancelledRunner(), "good": good_runner}

    with pytest.raises(RuntimeError, match="cancelled"):
        await registry.shutdown(timeout_s=0.01)

    assert good_runner.shutdown_called is True
    assert "good" not in registry._runners
    assert "cancelled" in registry._runners


@pytest.mark.anyio
async def test_codex_dispose_interrupts_remote_turn_before_detaching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex sidecar disposal interrupts the owned remote turn before detaching."""
    from tether.runner.codex_sdk_sidecar import SidecarRunner

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "tether.runner.codex_sdk_sidecar.settings.codex_sidecar_url",
        lambda: "http://127.0.0.1:9/base",
    )
    monkeypatch.setattr(
        "tether.runner.codex_sdk_sidecar.settings.codex_sidecar_token", lambda: None
    )
    runner = SidecarRunner(NoopRunnerEvents())
    task = asyncio.create_task(wait_forever())
    runner._streams["session-1"] = task
    calls: list[tuple[str, dict]] = []

    async def post_json(path: str, payload: dict) -> None:
        calls.append((path, payload))

    monkeypatch.setattr(runner, "_post_json", post_json)

    disposed = await runner.dispose_session("session-1")

    assert disposed is True
    assert calls == [("/sessions/interrupt", {"session_id": "session-1"})]
    assert task.done()


@pytest.mark.anyio
async def test_opencode_dispose_interrupts_remote_turn_before_detaching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenCode sidecar disposal interrupts the owned remote turn before detaching."""
    from tether.runner.opencode_sdk_sidecar import OpenCodeSidecarRunner

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    runner = OpenCodeSidecarRunner(NoopRunnerEvents())
    task = asyncio.create_task(wait_forever())
    runner._streams["session-1"] = task
    calls: list[tuple[str, dict]] = []

    async def post_json(path: str, payload: dict) -> None:
        calls.append((path, payload))

    monkeypatch.setattr(runner, "_post_json", post_json)

    disposed = await runner.dispose_session("session-1")

    assert disposed is True
    assert calls == [("/sessions/interrupt", {"session_id": "session-1"})]
    assert task.done()


@pytest.mark.anyio
async def test_automation_dispose_failure_keeps_process_handle(
    fresh_store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed automation disposal keeps process handles for later cleanup."""
    import tether.runner.automation as automation
    from tether.runner.automation import AutomationRunner

    class HungProcess:
        returncode: int | None = None

        def __init__(self) -> None:
            self.terminate_called = False
            self.kill_called = False

        def terminate(self) -> None:
            self.terminate_called = True

        def kill(self) -> None:
            self.kill_called = True

        async def wait(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(automation, "store", fresh_store)
    monkeypatch.setattr(automation, "AUTOMATION_TERMINATE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(automation, "AUTOMATION_KILL_TIMEOUT_SECONDS", 0.01)

    runner = AutomationRunner(NoopRunnerEvents())
    process = HungProcess()
    runner._processes["session-1"] = process
    fresh_store.set_process("session-1", process)

    with pytest.raises(RuntimeError, match="did not exit"):
        await runner.dispose_session("session-1")

    assert runner._processes["session-1"] is process
    assert fresh_store.get_process("session-1") is process
    assert process.terminate_called is True
    assert process.kill_called is True


@pytest.mark.anyio
async def test_idle_stop_is_bounded_and_reports_failure(
    fresh_store: SessionStore,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung idle-stop must not wedge every later maintenance pass."""
    import tether.maintenance as maintenance

    _patch_store(monkeypatch, fresh_store)
    session_id = _persist_session(fresh_store, SessionState.RUNNING)

    async def hung_stop(session_id: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(fake_runner, "stop", hung_stop, raising=False)
    monkeypatch.setattr(maintenance, "get_api_runner", lambda adapter: fake_runner)
    monkeypatch.setattr(maintenance, "RUNNER_DISPOSE_TIMEOUT_SECONDS", 0.01)

    await asyncio.wait_for(maintenance._stop_idle_session(session_id), timeout=0.2)

    assert fresh_store.get_session(session_id).state == SessionState.ERROR
    events = fresh_store.read_event_log(session_id)
    assert any(
        event.get("data", {}).get("code") == "RUNNER_STOP_FAILED" for event in events
    )


@pytest.mark.anyio
async def test_public_interrupt_is_bounded_and_retains_owned_process(
    api_client: httpx.AsyncClient,
    fresh_store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung stop reports failure, denies waiters, and preserves cleanup ownership."""
    from tether.api import sessions

    session_id = _persist_session(fresh_store, SessionState.RUNNING)
    process = FakeProcess()
    fresh_store.set_process(session_id, process)
    permission = asyncio.get_running_loop().create_future()
    fresh_store.add_pending_permission(session_id, "request", "Bash", {}, permission)
    cancelled = asyncio.Event()

    class HungRunner:
        async def stop(self, session_id: str) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    runner = HungRunner()
    monkeypatch.setattr(sessions, "get_api_runner", lambda adapter: runner)
    monkeypatch.setattr(sessions, "RUNNER_STOP_TIMEOUT_SECONDS", 0.01, raising=False)
    response = await asyncio.wait_for(
        api_client.post(f"/api/sessions/{session_id}/interrupt"), timeout=0.2
    )
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "RUNNER_STOP_FAILED"
    assert cancelled.is_set()
    assert fresh_store.get_session(session_id).state == SessionState.ERROR
    assert fresh_store.get_process(session_id) is process
    assert permission.done()
    assert permission.cancelled()
