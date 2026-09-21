"""Regression tests for pi RPC failure paths that can silence sessions."""

from __future__ import annotations

import asyncio

import pytest

from tether.models import SessionState
from tether.runner.base import RunnerUnavailableError
from tether.runner.pi_rpc import PiRpcRunner
from tether.store import SessionStore


class FakeEvents:
    """Minimal RunnerEvents recorder for pi RPC tests."""

    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []
        self.heartbeats: list[dict[str, str | bool]] = []
        self.awaiting_input_count = 0
        self.exit_count = 0

    async def on_error(self, session_id: str, code: str, message: str) -> None:
        self.errors.append({"session_id": session_id, "code": code, "message": message})
        await asyncio.sleep(0)

    async def on_heartbeat(self, session_id: str, elapsed_s: float, done: bool) -> None:
        self.heartbeats.append({"session_id": session_id, "done": done})

    async def on_awaiting_input(self, session_id: str) -> None:
        self.awaiting_input_count += 1

    async def on_exit(self, session_id: str, exit_code: int | None) -> None:
        self.exit_count += 1


class FailingStdin:
    """Fake stdin whose drain fails like a closed pipe."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        raise BrokenPipeError("closed")


class EmptyStdout:
    """Fake stdout that immediately reaches EOF."""

    async def readline(self) -> bytes:
        return b""


class EmptyStderr:
    """Fake stderr with no diagnostics."""

    async def read(self) -> bytes:
        return b""


class CleanExitProcess:
    """Fake pi process that exits cleanly without any RPC events."""

    def __init__(self) -> None:
        self.stdout = EmptyStdout()
        self.stderr = EmptyStderr()
        self.returncode = 0
        self.stdin = None

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


@pytest.mark.anyio
async def test_send_prompt_without_process_raises_unavailable() -> None:
    """Missing pi processes fail visibly instead of dropping prompts."""
    runner = PiRpcRunner(FakeEvents())

    with pytest.raises(RunnerUnavailableError, match="pi process is not available"):
        await runner._send_prompt("sess_missing", "hello")


@pytest.mark.anyio
async def test_write_command_drain_failure_raises_unavailable() -> None:
    """Closed pi stdin is reported to the caller."""
    runner = PiRpcRunner(FakeEvents())
    proc = CleanExitProcess()
    proc.stdin = FailingStdin()

    with pytest.raises(RunnerUnavailableError, match="failed to write to pi process"):
        await runner._write_cmd_async(proc, {"type": "prompt", "message": "hello"})


@pytest.mark.anyio
async def test_clean_eof_while_running_marks_session_error(
    fresh_store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean pi RPC EOF during a turn is not treated as successful input wait."""
    monkeypatch.setattr("tether.runner.pi_rpc.store", fresh_store)
    session = fresh_store.create_session(repo_id="repo", base_ref=None)
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)

    events = FakeEvents()
    runner = PiRpcRunner(events)
    proc = CleanExitProcess()

    await runner._read_events(session.id, proc)

    assert events.errors == [
        {
            "session_id": session.id,
            "code": "PI_PROCESS_EXITED",
            "message": "Pi process exited before completing the turn.",
        }
    ]
    assert events.awaiting_input_count == 0


@pytest.mark.anyio
async def test_reader_can_deliver_error_after_cleaning_up(
    fresh_store, monkeypatch
) -> None:
    """Reader cleanup must not cancel its own pending error delivery."""
    monkeypatch.setattr("tether.runner.pi_rpc.store", fresh_store)
    session = fresh_store.create_session(repo_id="repo", base_ref=None)
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)
    events = FakeEvents()
    runner = PiRpcRunner(events)
    task = asyncio.create_task(runner._read_events(session.id, CleanExitProcess()))
    runner._readers[session.id] = task

    await task

    assert events.errors[0]["code"] == "PI_PROCESS_EXITED"
    assert not task.cancelled()


@pytest.mark.anyio
async def test_failed_process_reports_diagnostic(fresh_store, monkeypatch) -> None:
    """An unavailable model's startup diagnostic reaches the bridge error event."""
    monkeypatch.setattr("tether.runner.pi_rpc.store", fresh_store)
    session = fresh_store.create_session(repo_id="repo", base_ref=None)
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)
    events = FakeEvents()
    runner = PiRpcRunner(events)
    proc = CleanExitProcess()
    proc.returncode = 1
    proc.stderr = asyncio.StreamReader()
    proc.stderr.feed_data(b"Model provider/missing not found\n")
    proc.stderr.feed_eof()

    await runner._read_events(session.id, proc)

    assert "provider/missing not found" in events.errors[0]["message"]
    assert "Check the model" in events.errors[0]["message"]
    assert events.exit_count == 1


@pytest.mark.anyio
async def test_stop_finishes_reader_before_returning(fresh_store, monkeypatch) -> None:
    """A model reset cannot leave an old reader to clean up the next process."""
    monkeypatch.setattr("tether.runner.pi_rpc.store", fresh_store)
    session = fresh_store.create_session(repo_id="repo", base_ref=None)
    events = FakeEvents()
    runner = PiRpcRunner(events)
    proc = CleanExitProcess()
    proc.stdout = asyncio.StreamReader()
    task = asyncio.create_task(runner._read_events(session.id, proc))
    runner._readers[session.id] = task
    runner._processes[session.id] = proc
    await asyncio.sleep(0)

    await runner.stop(session.id)

    assert task.done()
    assert not events.errors
    assert events.exit_count == 0
    assert session.id not in runner._processes
