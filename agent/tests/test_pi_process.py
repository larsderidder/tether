"""API-to-process integration tests using a deterministic Pi RPC executable."""

import asyncio
import shutil
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest

from tether.api.runner_events import ApiRunnerEvents
from tether.models import SessionState
from tether.runner.pi_rpc import PiRpcRunner
from tether.store import SessionStore


class ProcessEvents(ApiRunnerEvents):
    """Preserve actual API callbacks while exposing bounded completion waits."""

    def __init__(self) -> None:
        self.completed = asyncio.Event()
        self.failed = asyncio.Event()

    async def on_awaiting_input(self, session_id: str) -> None:
        await super().on_awaiting_input(session_id)
        self.completed.set()

    async def on_error(self, session_id: str, code: str, message: str) -> None:
        await super().on_error(session_id, code, message)
        self.failed.set()


@pytest.fixture
async def process_runner(
    fresh_store: SessionStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[PiRpcRunner, ProcessEvents]]:
    """Run a real subprocess, but never invoke the installed Pi binary."""
    binary = tmp_path / "fake-pi"
    shutil.copy2(Path(__file__).parent / "helpers" / "fake_pi.py", binary)
    binary.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("tether.runner.pi_rpc.store", fresh_store)
    monkeypatch.setattr(
        "tether.runner.pi_rpc.get_pi_session_detail", lambda *args: None
    )
    events = ProcessEvents()
    runner = PiRpcRunner(events)
    runner._pi_binary = str(binary)
    monkeypatch.setattr(
        "tether.api.sessions.get_api_runner", lambda *args, **kwargs: runner
    )
    try:
        yield runner, events
    finally:
        for session_id in list(runner._processes):
            await runner.stop(session_id)


async def create_session(client: httpx.AsyncClient, directory: Path) -> str:
    """Create through the public API so state, adapters and event routing are real."""
    response = await client.post(
        "/api/sessions", json={"directory": str(directory), "adapter": "pi_rpc"}
    )
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.anyio
async def test_real_process_completes_two_turns_without_duplicate_finals(
    api_client: httpx.AsyncClient,
    process_runner,
    fresh_store: SessionStore,
    tmp_path: Path,
) -> None:
    """A retained subprocess must accept another turn and publish each answer once."""
    runner, events = process_runner
    session_id = await create_session(api_client, tmp_path)
    first = await api_client.post(
        f"/api/sessions/{session_id}/start", json={"prompt": "first"}
    )
    assert first.status_code == 200
    await asyncio.wait_for(events.completed.wait(), timeout=5)
    process = runner._processes[session_id]
    events.completed.clear()
    second = await api_client.post(
        f"/api/sessions/{session_id}/input", json={"text": "second"}
    )
    assert second.status_code == 200
    await asyncio.wait_for(events.completed.wait(), timeout=5)
    assert runner._processes[session_id] is process
    assert fresh_store.get_session(session_id).state == SessionState.AWAITING_INPUT
    finals = [
        event["data"]["text"]
        for event in fresh_store.read_event_log(session_id)
        if event["type"] == "output_final"
    ]
    assert finals == ["answer: first", "answer: second"]


@pytest.mark.anyio
async def test_real_process_crash_is_visible_and_session_can_resume(
    api_client: httpx.AsyncClient,
    process_runner,
    fresh_store: SessionStore,
    tmp_path: Path,
) -> None:
    """A child crash must become ERROR, and a later input must spawn a working peer."""
    runner, events = process_runner
    session_id = await create_session(api_client, tmp_path)
    response = await api_client.post(
        f"/api/sessions/{session_id}/start", json={"prompt": "crash"}
    )
    assert response.status_code == 200
    await asyncio.wait_for(events.failed.wait(), timeout=5)
    assert fresh_store.get_session(session_id).state == SessionState.ERROR
    response = await api_client.post(
        f"/api/sessions/{session_id}/input", json={"text": "recovered"}
    )
    assert response.status_code == 200
    await asyncio.wait_for(events.completed.wait(), timeout=5)
    assert fresh_store.get_session(session_id).state == SessionState.AWAITING_INPUT
    assert session_id in runner._processes


@pytest.mark.anyio
async def test_real_process_interrupt_reaps_child_and_clears_runtime(
    api_client: httpx.AsyncClient,
    process_runner,
    fresh_store: SessionStore,
    tmp_path: Path,
) -> None:
    """Interrupting an actual child must not leave INTERRUPTING or reader tasks behind."""
    runner, _ = process_runner
    session_id = await create_session(api_client, tmp_path)
    response = await api_client.post(
        f"/api/sessions/{session_id}/start", json={"prompt": "hang"}
    )
    assert response.status_code == 200
    process = runner._processes[session_id]
    response = await asyncio.wait_for(
        api_client.post(f"/api/sessions/{session_id}/interrupt"), timeout=10
    )
    assert response.status_code == 200
    assert process.returncode == 0
    assert fresh_store.get_session(session_id).state == SessionState.AWAITING_INPUT
    assert session_id not in runner._processes
    assert session_id not in runner._readers
    assert session_id not in runner._heartbeat_tasks
    assert fresh_store.get_process(session_id) is None


@pytest.mark.anyio
async def test_real_process_stderr_pressure_does_not_block_output(
    api_client: httpx.AsyncClient,
    process_runner,
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnostics exceeding a pipe buffer must not deadlock an otherwise healthy turn."""
    monkeypatch.setattr("tether.runner.pi_rpc._PI_RPC_STREAM_LIMIT_BYTES", 4096)
    _, events = process_runner
    session_id = await create_session(api_client, tmp_path)
    response = await api_client.post(
        f"/api/sessions/{session_id}/start", json={"prompt": "noisy"}
    )
    assert response.status_code == 200
    await asyncio.wait_for(events.completed.wait(), timeout=5)
    assert fresh_store.get_session(session_id).state == SessionState.AWAITING_INPUT


@pytest.mark.anyio
async def test_real_process_disposal_reports_owned_resources(
    api_client: httpx.AsyncClient, process_runner, tmp_path: Path
) -> None:
    """Disposal must implement the lifecycle boolean contract and remain idempotent."""
    runner, _ = process_runner
    session_id = await create_session(api_client, tmp_path)
    await api_client.post(f"/api/sessions/{session_id}/start", json={"prompt": "hang"})
    process = runner._processes[session_id]

    assert await runner.dispose_session(session_id) is True
    assert process.returncode == 0
    assert await runner.dispose_session(session_id) is False


@pytest.mark.anyio
async def test_shutdown_starts_cleanup_for_every_owned_pi_process(
    api_client: httpx.AsyncClient,
    process_runner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked cleanup must not prevent other owned children being stopped."""
    runner, _ = process_runner
    session_ids = [await create_session(api_client, tmp_path) for _ in range(2)]
    for session_id in session_ids:
        await api_client.post(
            f"/api/sessions/{session_id}/start", json={"prompt": "hang"}
        )
    processes = list(runner._processes.values())
    started: set[str] = set()
    all_started = asyncio.Event()
    original_dispose = runner.dispose_session

    async def dispose_after_all_started(session_id: str) -> bool:
        started.add(session_id)
        if started == set(session_ids):
            all_started.set()
        await all_started.wait()
        return await original_dispose(session_id)

    with monkeypatch.context() as patch:
        patch.setattr(runner, "dispose_session", dispose_after_all_started)
        await asyncio.wait_for(runner.shutdown(), timeout=2)

    assert all(process.returncode == 0 for process in processes)
    assert not runner._processes
    assert not runner._readers


@pytest.mark.anyio
async def test_registry_shutdown_allows_pi_kill_escalation(
    api_client: httpx.AsyncClient, process_runner, tmp_path: Path
) -> None:
    """The outer shutdown deadline must outlast Pi's graceful-abort deadline."""
    from tether.api.runner_registry import RunnerRegistry

    runner, _ = process_runner
    session_id = await create_session(api_client, tmp_path)
    await api_client.post(
        f"/api/sessions/{session_id}/start", json={"prompt": "ignore-abort"}
    )
    process = runner._processes[session_id]
    registry = RunnerRegistry(ProcessEvents())
    registry._runners = {"pi_rpc": runner}

    await asyncio.wait_for(registry.shutdown(), timeout=10)

    assert process.returncode is not None
    assert process.returncode != 0
    assert not registry._runners
    assert not runner._processes
