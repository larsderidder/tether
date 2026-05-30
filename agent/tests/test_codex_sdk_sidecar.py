"""Tests for the Codex sidecar runner's CLI fallback path."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from tether.runner.base import RunnerUnavailableError


class _FakeProcess:
    """Minimal asyncio.subprocess.Process double for Codex CLI tests."""

    def __init__(self, events: list[dict] | None = None, *, block: bool = True):
        self.returncode: int | None = None
        self.pid = 4242
        self.terminated = False
        self.killed = False
        self.stdin = _FakeStreamWriter()
        self.stdout = _FakeStreamReader(events or [], block_after=block, on_eof=self._auto_finish)
        self.stderr = _FakeStreamReader([], block_after=False)
        self._wait_event = asyncio.Event()

    async def wait(self) -> int:
        if self.returncode is not None:
            return self.returncode
        await self._wait_event.wait()
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.stdout.finish()
        self._wait_event.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.finish()
        self._wait_event.set()

    def _auto_finish(self) -> None:
        if self.returncode is None:
            self.returncode = 0
        self._wait_event.set()


class _FakeStreamReader:
    def __init__(self, events: list[dict], *, block_after: bool, on_eof=None):
        self._lines = [
            (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
            for event in events
        ]
        self._index = 0
        self._block_after = block_after
        self._more = asyncio.Event()
        self._on_eof = on_eof

    async def readline(self) -> bytes:
        if self._index < len(self._lines):
            line = self._lines[self._index]
            self._index += 1
            return line
        if self._block_after and not self._more.is_set():
            await self._more.wait()
        if self._on_eof:
            self._on_eof()
        return b""

    async def read(self) -> bytes:
        return b""

    def finish(self) -> None:
        self._more.set()


class _FakeStreamWriter:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.eof = False

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        pass

    def can_write_eof(self) -> bool:
        return True

    def write_eof(self) -> None:
        self.eof = True

    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8")


def _make_events() -> MagicMock:
    events = MagicMock()
    events.on_output = AsyncMock()
    events.on_error = AsyncMock()
    events.on_header = AsyncMock()
    events.on_metadata = AsyncMock()
    events.on_heartbeat = AsyncMock()
    events.on_awaiting_input = AsyncMock()
    events.on_exit = AsyncMock()
    events.on_permission_request = AsyncMock()
    events.on_permission_resolved = AsyncMock()
    return events


@pytest.fixture
def runner(monkeypatch, fresh_store, tmp_path):
    monkeypatch.setattr("tether.runner.codex_sdk_sidecar.store", fresh_store)

    from tether.runner.codex_sdk_sidecar import SidecarRunner

    events = _make_events()
    runner = SidecarRunner(events)
    session = fresh_store.create_session(str(tmp_path), None)
    session.directory = str(tmp_path)
    session.approval_mode = 2
    fresh_store.update_session(session)
    fresh_store.set_workdir(session.id, str(tmp_path), managed=False)
    return runner, session, events


@pytest.mark.anyio
async def test_codex_runner_falls_back_to_cli_on_start(monkeypatch, runner):
    runner, session, events = runner
    fake_proc = _FakeProcess(
        [
            {"type": "thread.started", "thread_id": "thread_abc"},
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "ok"},
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1,
                    "cached_input_tokens": 2,
                    "output_tokens": 3,
                },
            },
        ],
        block=False,
    )
    spawned: list[tuple[tuple, dict]] = []

    async def _create_subprocess(*args, **kwargs):
        spawned.append((args, kwargs))
        return fake_proc

    monkeypatch.setattr(
        runner,
        "_post_json",
        AsyncMock(side_effect=RunnerUnavailableError("sidecar down")),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess)

    await runner.start(session.id, "hello from tether", approval_choice=2)
    await asyncio.sleep(0.05)

    args, kwargs = spawned[0]
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE
    assert args[0] == "codex"
    assert args[1:3] == ("-C", session.directory)
    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert "exec" in args
    assert "--json" in args
    assert fake_proc.stdin.text() == "hello from tether"
    assert fake_proc.stdin.eof is True

    events.on_header.assert_awaited_once()
    assert events.on_header.await_args.kwargs["thread_id"] == "thread_abc"
    assert events.on_header.await_args.kwargs["provider"] == "OpenAI (Codex CLI)"

    final_call = events.on_output.await_args_list[0]
    assert final_call.args[2] == "ok"
    assert final_call.kwargs["kind"] == "final"
    assert final_call.kwargs["is_final"] is True

    metadata_keys = [call.args[1] for call in events.on_metadata.await_args_list]
    assert metadata_keys == [
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "tokens_used",
    ]
    events.on_awaiting_input.assert_awaited_once_with(session.id)


@pytest.mark.anyio
async def test_codex_runner_falls_back_to_cli_on_405_sidecar_response(
    monkeypatch, runner
):
    runner, session, events = runner
    fake_proc = _FakeProcess(
        [
            {"type": "thread.started", "thread_id": "thread_405"},
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "ok"},
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                },
            },
        ],
        block=False,
    )
    spawned: list[tuple[tuple, dict]] = []

    async def _create_subprocess(*args, **kwargs):
        spawned.append((args, kwargs))
        return fake_proc

    monkeypatch.setattr(
        runner,
        "_post_json",
        AsyncMock(
            side_effect=RuntimeError(
                "Sidecar request failed: 405 Request method must be `GET`"
            )
        ),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess)

    await runner.start(session.id, "hello from tether", approval_choice=2)
    await asyncio.sleep(0.05)

    assert spawned
    events.on_header.assert_awaited_once()
    assert events.on_header.await_args.kwargs["thread_id"] == "thread_405"


@pytest.mark.anyio
async def test_codex_runner_cli_resume_uses_runner_session_id(
    monkeypatch, runner, fresh_store
):
    runner, session, events = runner
    fake_proc = _FakeProcess(
        [
            {"type": "thread.started", "thread_id": "thread_existing"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "agent_message",
                    "text": "resumed",
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 5,
                    "cached_input_tokens": 0,
                    "output_tokens": 7,
                },
            },
        ],
        block=False,
    )
    spawned: list[tuple[tuple, dict]] = []

    async def _create_subprocess(*args, **kwargs):
        spawned.append((args, kwargs))
        return fake_proc

    monkeypatch.setattr(
        runner,
        "_post_json",
        AsyncMock(side_effect=RunnerUnavailableError("sidecar down")),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess)

    session.approval_mode = 1
    fresh_store.update_session(session)
    fresh_store.set_runner_session_id(session.id, "thread_existing")

    await runner.send_input(session.id, "follow-up input")
    await asyncio.sleep(0.05)

    args, _kwargs = spawned[0]
    assert "resume" in args
    resume_index = args.index("resume")
    assert args[resume_index + 1] == "thread_existing"
    assert "-a" in args
    assert args[args.index("-a") + 1] == "on-failure"
    assert fake_proc.stdin.text() == "follow-up input"
    assert events.on_header.await_args.kwargs["approval"] == "on-failure"


@pytest.mark.anyio
async def test_codex_runner_cli_stop_interrupts_local_process(monkeypatch, runner):
    runner, session, events = runner
    fake_proc = _FakeProcess([], block=True)

    async def _create_subprocess(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(
        runner,
        "_post_json",
        AsyncMock(side_effect=RunnerUnavailableError("sidecar down")),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess)

    await runner.start(session.id, "long running turn", approval_choice=2)
    await runner.stop(session.id)

    assert fake_proc.terminated is True
    events.on_exit.assert_awaited_once_with(session.id, -15)
    events.on_awaiting_input.assert_not_awaited()
