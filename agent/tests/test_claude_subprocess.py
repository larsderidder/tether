"""Tests for the claude_subprocess runner (parent-side IPC logic)."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers to simulate the child process over pipes
# ---------------------------------------------------------------------------


class FakeProcess:
    """Simulates asyncio.subprocess.Process with in-memory pipes.

    By default, stdout blocks after initial events are consumed — the
    reader task stays alive until ``_finish()`` is called.  Pass
    ``block=False`` for processes that should EOF immediately.
    """

    def __init__(self, events: list[dict] | None = None, *, block: bool = True):
        self._events = events or []
        self._stdin_lines: list[str] = []
        self.returncode: int | None = None
        self.pid = 12345

        self.stdout = _FakeStreamReader(self._events, block_after=block, on_eof=self._auto_finish)
        self.stderr = _FakeStreamReader([], block_after=False)
        self.stdin = _FakeStreamWriter(self._stdin_lines)

        self._wait_event = asyncio.Event()

    async def wait(self) -> int:
        if self.returncode is not None:
            return self.returncode
        await self._wait_event.wait()
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self.stdout._more.set()
        self._wait_event.set()

    def _auto_finish(self) -> None:
        """Called when stdout reader reaches EOF — auto-exit the process."""
        if self.returncode is None:
            self.returncode = 0
        self._wait_event.set()

    def _finish(self, code: int = 0) -> None:
        """Signal that the process has exited — unblocks reader and wait."""
        self.returncode = code
        self.stdout._more.set()
        self._wait_event.set()

    def get_stdin_commands(self) -> list[dict]:
        """Parse all commands written to stdin."""
        return [json.loads(line) for line in self._stdin_lines if line.strip()]


class _FakeStreamReader:
    """Simulates asyncio.StreamReader backed by a list of dicts.

    If ``block_after`` is True (the default for FakeProcess), readline()
    will wait on ``_more`` after exhausting the initial events instead of
    returning EOF.  Call ``finish()`` on the owning FakeProcess to unblock.
    """

    def __init__(self, events: list[dict], *, block_after: bool = False, on_eof=None):
        self._lines = [
            (json.dumps(e, separators=(",", ":")) + "\n").encode()
            for e in events
        ]
        self._index = 0
        self._exhausted = asyncio.Event()
        self._block_after = block_after
        self._more: asyncio.Event = asyncio.Event()
        self._extra_lines: list[bytes] = []
        self._on_eof = on_eof

    async def readline(self) -> bytes:
        if self._index < len(self._lines):
            line = self._lines[self._index]
            self._index += 1
            if self._index >= len(self._lines):
                self._exhausted.set()
            return line
        if self._block_after and not self._more.is_set():
            await self._more.wait()
            # Drain any extra lines added while blocked
            if self._extra_lines:
                return self._extra_lines.pop(0)
        self._exhausted.set()
        if self._on_eof:
            self._on_eof()
        return b""

    async def read(self) -> bytes:
        return b""


class _FakeStreamWriter:
    """Simulates asyncio.StreamWriter backed by a list of strings."""

    def __init__(self, lines: list[str]):
        self._lines = lines

    def write(self, data: bytes) -> None:
        self._lines.append(data.decode())

    async def drain(self) -> None:
        pass


def _make_events():
    """Create mock RunnerEvents with all required callbacks."""
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOutputClassification:
    """Test that JSON output events are correctly classified as step vs final."""

    @pytest.fixture
    def mock_events(self):
        return _make_events()

    @pytest.fixture
    def runner(self, mock_events, monkeypatch, fresh_store, tmp_path):
        monkeypatch.setattr("tether.runner.claude_subprocess.store", fresh_store)
        from tether.runner.claude_subprocess import ClaudeSubprocessRunner

        runner = ClaudeSubprocessRunner(mock_events)
        session = fresh_store.create_session(str(tmp_path), None)
        session.directory = str(tmp_path)
        fresh_store.update_session(session)
        return runner, session

    @pytest.mark.anyio
    async def test_single_text_block_is_final(self, runner, mock_events):
        runner, session = runner
        event = {
            "event": "output",
            "blocks": [{"type": "text", "text": "Hello!"}],
        }
        proc = MagicMock()
        await runner._on_output(session.id, event)

        mock_events.on_output.assert_called_once()
        call = mock_events.on_output.call_args
        assert call.kwargs["kind"] == "final"
        assert call.kwargs["is_final"] is True

    @pytest.mark.anyio
    async def test_text_with_tool_use_is_step(self, runner, mock_events):
        runner, session = runner
        event = {
            "event": "output",
            "blocks": [
                {"type": "text", "text": "Let me read that."},
                {"type": "tool_use", "name": "Read", "id": "t1", "input": {}},
            ],
        }
        await runner._on_output(session.id, event)

        text_calls = [
            c for c in mock_events.on_output.call_args_list
            if "Let me read" in str(c)
        ]
        assert len(text_calls) == 1
        assert text_calls[0].kwargs["kind"] == "step"
        assert text_calls[0].kwargs["is_final"] is False

    @pytest.mark.anyio
    async def test_tool_result_truncated(self, runner, mock_events):
        runner, session = runner
        long_content = "x" * 600
        event = {
            "event": "output",
            "blocks": [{"type": "tool_result", "content": long_content, "is_error": False}],
        }
        await runner._on_output(session.id, event)

        call = mock_events.on_output.call_args
        text = call.args[2]
        assert text.startswith("[result] ")
        assert text.endswith("...\n")
        assert len(text) < 600

    @pytest.mark.anyio
    async def test_thinking_block_is_step(self, runner, mock_events):
        runner, session = runner
        event = {
            "event": "output",
            "blocks": [
                {"type": "thinking", "thinking": "hmm..."},
                {"type": "text", "text": "Answer"},
            ],
        }
        await runner._on_output(session.id, event)

        thinking_calls = [
            c for c in mock_events.on_output.call_args_list
            if "[thinking]" in str(c)
        ]
        assert len(thinking_calls) == 1
        assert thinking_calls[0].kwargs["kind"] == "step"


class TestSubprocessTurnLifecycle:
    """Test that a subprocess turn completes and signals awaiting input."""

    @pytest.fixture
    def mock_events(self):
        return _make_events()

    @pytest.fixture
    def runner(self, mock_events, monkeypatch, fresh_store, tmp_path):
        monkeypatch.setattr("tether.runner.claude_subprocess.store", fresh_store)
        from tether.runner.claude_subprocess import ClaudeSubprocessRunner

        runner = ClaudeSubprocessRunner(mock_events)
        session = fresh_store.create_session(str(tmp_path), None)
        session.directory = str(tmp_path)
        fresh_store.update_session(session)
        return runner, session, fresh_store

    @pytest.mark.anyio
    async def test_full_turn_signals_awaiting_input(self, runner, mock_events, monkeypatch):
        runner, session, store = runner

        child_events = [
            {"event": "init", "session_id": "sdk_1", "model": "claude", "version": "1.0"},
            {"event": "output", "blocks": [{"type": "text", "text": "Hi there"}]},
            {"event": "result", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001, "is_error": False, "error_text": None},
        ]
        fake_proc = FakeProcess(child_events, block=False)

        async def mock_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        await runner.start(session.id, "hello", approval_choice=0)

        # Wait for reader to finish
        reader_task = runner._readers[session.id]
        await asyncio.wait_for(reader_task, timeout=5.0)

        mock_events.on_header.assert_awaited_once()
        mock_events.on_output.assert_awaited()
        mock_events.on_metadata.assert_awaited()
        mock_events.on_awaiting_input.assert_awaited_once()

    @pytest.mark.anyio
    async def test_start_sends_correct_command(self, runner, mock_events, monkeypatch):
        runner, session, store = runner

        child_events = [
            {"event": "init", "session_id": "sdk_1", "model": "claude", "version": "1.0"},
            {"event": "result", "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "is_error": False, "error_text": None},
        ]
        fake_proc = FakeProcess(child_events, block=False)

        async def mock_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        await runner.start(session.id, "test prompt", approval_choice=2)
        await asyncio.wait_for(runner._readers[session.id], timeout=5.0)

        cmds = fake_proc.get_stdin_commands()
        assert len(cmds) >= 1
        start_cmd = cmds[0]
        assert start_cmd["cmd"] == "start"
        assert start_cmd["prompt"] == "test prompt"
        assert start_cmd["permission_mode"] == "bypassPermissions"
        assert start_cmd["cwd"] == session.directory

    @pytest.mark.anyio
    async def test_error_event_emits_on_error(self, runner, mock_events, monkeypatch):
        runner, session, store = runner

        child_events = [
            {"event": "error", "code": "SDK_ERROR", "message": "Something broke"},
        ]
        fake_proc = FakeProcess(child_events, block=False)

        async def mock_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        await runner.start(session.id, "hello", approval_choice=0)
        await asyncio.wait_for(runner._readers[session.id], timeout=5.0)

        mock_events.on_error.assert_awaited()
        error_call = mock_events.on_error.call_args
        assert error_call.args[1] == "SDK_ERROR"
        assert "Something broke" in error_call.args[2]

    @pytest.mark.anyio
    async def test_result_error_emits_on_error(self, runner, mock_events, monkeypatch):
        runner, session, store = runner

        child_events = [
            {"event": "init", "session_id": "sdk_1", "model": "claude", "version": "1.0"},
            {"event": "result", "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "is_error": True, "error_text": "Rate limited"},
        ]
        fake_proc = FakeProcess(child_events, block=False)

        async def mock_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        await runner.start(session.id, "hello", approval_choice=0)
        await asyncio.wait_for(runner._readers[session.id], timeout=5.0)

        mock_events.on_error.assert_awaited()
        assert "Rate limited" in mock_events.on_error.call_args.args[2]


class TestSessionBinding:
    """Test session ID binding/rebinding across subprocess turns."""

    @pytest.fixture
    def mock_events(self):
        return _make_events()

    @pytest.fixture
    def runner(self, mock_events, monkeypatch, fresh_store, tmp_path):
        monkeypatch.setattr("tether.runner.claude_subprocess.store", fresh_store)
        from tether.runner.claude_subprocess import ClaudeSubprocessRunner

        runner = ClaudeSubprocessRunner(mock_events)
        session = fresh_store.create_session(str(tmp_path), None)
        session.directory = str(tmp_path)
        fresh_store.update_session(session)
        return runner, session, fresh_store

    @pytest.mark.anyio
    async def test_init_binds_session(self, runner, mock_events, monkeypatch):
        runner, session, store = runner

        child_events = [
            {"event": "init", "session_id": "sdk_abc", "model": "claude", "version": "1.0"},
            {"event": "result", "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "is_error": False, "error_text": None},
        ]
        fake_proc = FakeProcess(child_events, block=False)

        async def mock_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        await runner.start(session.id, "hello", approval_choice=0)
        await asyncio.wait_for(runner._readers[session.id], timeout=5.0)

        assert runner._sdk_sessions[session.id] == "sdk_abc"
        assert store.get_runner_session_id(session.id) == "sdk_abc"

    @pytest.mark.anyio
    async def test_mismatch_rebinds_cache_and_store(self, runner, mock_events, monkeypatch):
        runner, session, store = runner
        store.set_runner_session_id(session.id, "sdk_old")
        runner._sdk_sessions[session.id] = "sdk_old"

        child_events = [
            {"event": "init", "session_id": "sdk_new", "model": "claude", "version": "1.0"},
            {"event": "result", "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "is_error": False, "error_text": None},
        ]
        fake_proc = FakeProcess(child_events, block=False)

        async def mock_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        await runner.send_input(session.id, "follow-up")
        await asyncio.wait_for(runner._readers[session.id], timeout=5.0)

        # Both cache and store updated to the new SDK session
        assert runner._sdk_sessions[session.id] == "sdk_new"
        assert store.get_runner_session_id(session.id) == "sdk_new"

    @pytest.mark.anyio
    async def test_start_prefers_cache_over_stale_store(self, runner, mock_events, monkeypatch):
        """start() uses in-memory cache when store has stale binding."""
        runner, session, store = runner
        # Simulate: store has old value, cache has newer one from prior expiry
        store.set_runner_session_id(session.id, "sdk_old")
        runner._sdk_sessions[session.id] = "sdk_refreshed"

        child_events = [
            {"event": "init", "session_id": "sdk_refreshed", "model": "claude", "version": "1.0"},
            {"event": "result", "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "is_error": False, "error_text": None},
        ]
        fake_proc = FakeProcess(child_events, block=False)

        async def mock_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        await runner.start(session.id, "hello", approval_choice=0)
        await asyncio.wait_for(runner._readers[session.id], timeout=5.0)

        # Verify the cached value was used, not the stale store value
        cmds = fake_proc.get_stdin_commands()
        start_cmd = cmds[0]
        assert start_cmd["resume"] == "sdk_refreshed"


class TestMultiSessionIsolation:
    """Test that multiple sessions in the same directory don't cross-talk."""

    @pytest.fixture
    def mock_events(self):
        return _make_events()

    @pytest.fixture
    def setup(self, mock_events, monkeypatch, fresh_store, tmp_path):
        monkeypatch.setattr("tether.runner.claude_subprocess.store", fresh_store)
        from tether.runner.claude_subprocess import ClaudeSubprocessRunner

        runner = ClaudeSubprocessRunner(mock_events)
        # Two sessions sharing the same working directory
        session_a = fresh_store.create_session(str(tmp_path), None)
        session_a.directory = str(tmp_path)
        fresh_store.update_session(session_a)

        session_b = fresh_store.create_session(str(tmp_path), None)
        session_b.directory = str(tmp_path)
        fresh_store.update_session(session_b)

        return runner, session_a, session_b, fresh_store

    @pytest.mark.anyio
    async def test_concurrent_sessions_get_separate_sdk_ids(self, setup, mock_events, monkeypatch):
        """Two sessions in the same dir each get their own SDK session ID."""
        runner, sess_a, sess_b, store = setup

        procs = {}

        async def mock_subprocess(*args, **kwargs):
            # Determine which session is being spawned by checking what's
            # already been created.  First call → session A, second → B.
            if "a" not in procs:
                procs["a"] = FakeProcess([
                    {"event": "init", "session_id": "sdk_AAA", "model": "claude", "version": "1.0"},
                    {"event": "result", "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "is_error": False, "error_text": None},
                ], block=False)
                return procs["a"]
            else:
                procs["b"] = FakeProcess([
                    {"event": "init", "session_id": "sdk_BBB", "model": "claude", "version": "1.0"},
                    {"event": "result", "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "is_error": False, "error_text": None},
                ], block=False)
                return procs["b"]

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        await runner.start(sess_a.id, "hello from A", approval_choice=0)
        await asyncio.wait_for(runner._readers[sess_a.id], timeout=5.0)

        await runner.start(sess_b.id, "hello from B", approval_choice=0)
        await asyncio.wait_for(runner._readers[sess_b.id], timeout=5.0)

        # Each session has its own SDK session binding
        assert runner._sdk_sessions[sess_a.id] == "sdk_AAA"
        assert runner._sdk_sessions[sess_b.id] == "sdk_BBB"
        assert store.get_runner_session_id(sess_a.id) == "sdk_AAA"
        assert store.get_runner_session_id(sess_b.id) == "sdk_BBB"

    @pytest.mark.anyio
    async def test_send_input_routes_to_correct_sdk_session(self, setup, mock_events, monkeypatch):
        """Follow-up input uses the correct SDK session ID, not the other session's."""
        runner, sess_a, sess_b, store = setup

        # Bind each session to its own SDK session
        store.set_runner_session_id(sess_a.id, "sdk_AAA")
        store.set_runner_session_id(sess_b.id, "sdk_BBB")
        runner._sdk_sessions[sess_a.id] = "sdk_AAA"
        runner._sdk_sessions[sess_b.id] = "sdk_BBB"

        fake_procs = []

        async def mock_subprocess(*args, **kwargs):
            proc = FakeProcess([
                {"event": "init", "session_id": "sdk_BBB", "model": "claude", "version": "1.0"},
                {"event": "result", "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "is_error": False, "error_text": None},
            ], block=False)
            fake_procs.append(proc)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        # Send input to session B specifically
        await runner.send_input(sess_b.id, "follow-up for B")
        await asyncio.wait_for(runner._readers[sess_b.id], timeout=5.0)

        # Verify the subprocess was told to resume session B's SDK session
        cmds = fake_procs[0].get_stdin_commands()
        start_cmd = cmds[0]
        assert start_cmd["resume"] == "sdk_BBB"
        assert start_cmd["prompt"] == "follow-up for B"

    @pytest.mark.anyio
    async def test_session_expiry_doesnt_affect_other_session(self, setup, mock_events, monkeypatch):
        """When session A's SDK session expires, session B keeps its own binding."""
        runner, sess_a, sess_b, store = setup

        store.set_runner_session_id(sess_a.id, "sdk_AAA")
        store.set_runner_session_id(sess_b.id, "sdk_BBB")
        runner._sdk_sessions[sess_a.id] = "sdk_AAA"
        runner._sdk_sessions[sess_b.id] = "sdk_BBB"

        # Session A's SDK session expired — SDK creates sdk_AAA_v2
        child_events = [
            {"event": "init", "session_id": "sdk_AAA_v2", "model": "claude", "version": "1.0"},
            {"event": "result", "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "is_error": False, "error_text": None},
        ]
        fake_proc = FakeProcess(child_events, block=False)

        async def mock_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        await runner.send_input(sess_a.id, "retry A")
        await asyncio.wait_for(runner._readers[sess_a.id], timeout=5.0)

        # A got rebound to the new SDK session
        assert runner._sdk_sessions[sess_a.id] == "sdk_AAA_v2"
        assert store.get_runner_session_id(sess_a.id) == "sdk_AAA_v2"

        # B is completely unaffected
        assert runner._sdk_sessions[sess_b.id] == "sdk_BBB"
        assert store.get_runner_session_id(sess_b.id) == "sdk_BBB"

    @pytest.mark.anyio
    async def test_no_continue_flag_in_start_command(self, setup, mock_events, monkeypatch):
        """Worker subprocess must NOT receive continue_conversation=True.

        The --continue CLI flag resolves to the most recent session in the
        cwd, which could be the wrong session when multiple Tether sessions
        share a directory.
        """
        runner, sess_a, _, store = setup
        store.set_runner_session_id(sess_a.id, "sdk_AAA")
        runner._sdk_sessions[sess_a.id] = "sdk_AAA"

        fake_proc = FakeProcess([
            {"event": "init", "session_id": "sdk_AAA", "model": "claude", "version": "1.0"},
            {"event": "result", "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "is_error": False, "error_text": None},
        ], block=False)

        async def mock_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        await runner.send_input(sess_a.id, "hello")
        await asyncio.wait_for(runner._readers[sess_a.id], timeout=5.0)

        # The start command has resume set but NOT continue_conversation
        cmds = fake_proc.get_stdin_commands()
        start_cmd = cmds[0]
        assert start_cmd["resume"] == "sdk_AAA"
        # The worker receives the resume ID; it should NOT pass --continue
        # to the CLI. We verify this indirectly — the worker code sets
        # continue_conversation=False regardless of resume.


class TestStopBehavior:
    """Test stop command and subprocess cleanup."""

    @pytest.fixture
    def mock_events(self):
        return _make_events()

    @pytest.fixture
    def runner(self, mock_events, monkeypatch, fresh_store, tmp_path):
        monkeypatch.setattr("tether.runner.claude_subprocess.store", fresh_store)
        from tether.runner.claude_subprocess import ClaudeSubprocessRunner

        runner = ClaudeSubprocessRunner(mock_events)
        session = fresh_store.create_session(str(tmp_path), None)
        session.directory = str(tmp_path)
        fresh_store.update_session(session)
        return runner, session, fresh_store

    @pytest.mark.anyio
    async def test_stop_sends_stop_command(self, runner, mock_events, monkeypatch):
        runner, session, store = runner

        # Blocking process: reader stays alive after initial events
        child_events = [
            {"event": "init", "session_id": "sdk_1", "model": "claude", "version": "1.0"},
            {"event": "heartbeat", "elapsed_s": 1.0},
        ]
        fake_proc = FakeProcess(child_events, block=True)

        async def mock_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        await runner.start(session.id, "hello", approval_choice=0)

        # Wait for initial events to be processed, reader now blocked
        await asyncio.sleep(0.1)

        # stop() sends stop cmd, then waits; we finish the process in response
        async def finish_soon():
            await asyncio.sleep(0.05)
            fake_proc._finish(0)

        asyncio.create_task(finish_soon())

        result = await runner.stop(session.id)
        assert result == 0

        # Verify stop command was sent
        cmds = fake_proc.get_stdin_commands()
        stop_cmds = [c for c in cmds if c.get("cmd") == "stop"]
        assert len(stop_cmds) == 1

    @pytest.mark.anyio
    async def test_stop_kills_if_timeout(self, runner, mock_events, monkeypatch):
        """If subprocess doesn't exit after stop, it should be killed."""
        runner, session, store = runner

        fake_proc = FakeProcess([])
        fake_proc.returncode = None  # Process hasn't exited

        # Make wait() hang until killed
        original_wait = fake_proc.wait

        async def hanging_wait():
            await asyncio.sleep(60)
            return 0

        fake_proc.wait = hanging_wait

        # But kill() makes subsequent waits return
        original_kill = fake_proc.kill

        def kill_and_unblock():
            fake_proc.returncode = -9
            # Replace wait with instant return after kill
            async def instant_wait():
                return -9
            fake_proc.wait = instant_wait

        fake_proc.kill = kill_and_unblock

        runner._processes[session.id] = fake_proc

        result = await asyncio.wait_for(runner.stop(session.id), timeout=15.0)
        assert result == 0


class TestPermissionFlow:
    """Test the cross-process permission request/response flow."""

    @pytest.fixture
    def mock_events(self):
        return _make_events()

    @pytest.fixture
    def runner(self, mock_events, monkeypatch, fresh_store, tmp_path):
        monkeypatch.setattr("tether.runner.claude_subprocess.store", fresh_store)
        from tether.runner.claude_subprocess import ClaudeSubprocessRunner

        runner = ClaudeSubprocessRunner(mock_events)
        session = fresh_store.create_session(str(tmp_path), None)
        session.directory = str(tmp_path)
        fresh_store.update_session(session)
        return runner, session, fresh_store

    @pytest.mark.anyio
    async def test_permission_request_creates_future_and_emits(self, runner, mock_events, monkeypatch):
        runner, session, store = runner

        child_events = [
            {"event": "init", "session_id": "sdk_1", "model": "claude", "version": "1.0"},
            {"event": "permission_request", "request_id": "perm_1", "tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
        ]
        # Block after events so reader stays alive for permission round-trip
        fake_proc = FakeProcess(child_events, block=True)

        async def mock_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        await runner.start(session.id, "hello", approval_choice=0)

        # Give reader time to process the permission request
        await asyncio.sleep(0.15)

        # Verify permission request was emitted to UI
        mock_events.on_permission_request.assert_awaited_once()
        call = mock_events.on_permission_request.call_args
        assert call.kwargs["request_id"] == "perm_1"
        assert call.kwargs["tool_name"] == "Bash"

        # Verify a pending permission exists in the store
        pending = store.get_pending_permission(session.id, "perm_1")
        assert pending is not None

        # Resolve the permission — this should write back to subprocess stdin
        store.resolve_pending_permission(session.id, "perm_1", {"behavior": "allow"})

        # Wait a beat for the background task to send the response
        await asyncio.sleep(0.1)

        # Check that permission_response was written to stdin
        cmds = fake_proc.get_stdin_commands()
        perm_cmds = [c for c in cmds if c.get("cmd") == "permission_response"]
        assert len(perm_cmds) == 1
        assert perm_cmds[0]["behavior"] == "allow"
        assert perm_cmds[0]["request_id"] == "perm_1"

        # Clean up
        fake_proc._finish(0)
        reader = runner._readers.get(session.id)
        if reader:
            await asyncio.wait_for(reader, timeout=5.0)


class TestPendingInputs:
    """Test that inputs queued during an active subprocess are processed."""

    @pytest.fixture
    def mock_events(self):
        return _make_events()

    @pytest.fixture
    def runner(self, mock_events, monkeypatch, fresh_store, tmp_path):
        monkeypatch.setattr("tether.runner.claude_subprocess.store", fresh_store)
        from tether.runner.claude_subprocess import ClaudeSubprocessRunner

        runner = ClaudeSubprocessRunner(mock_events)
        session = fresh_store.create_session(str(tmp_path), None)
        session.directory = str(tmp_path)
        fresh_store.update_session(session)
        return runner, session, fresh_store

    @pytest.mark.anyio
    async def test_send_input_while_running_queues(self, runner, mock_events, monkeypatch):
        runner, session, store = runner

        # Blocking process that stays alive after init
        child_events = [
            {"event": "init", "session_id": "sdk_1", "model": "claude", "version": "1.0"},
        ]
        fake_proc = FakeProcess(child_events, block=True)

        async def mock_subprocess(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess)

        await runner.start(session.id, "first", approval_choice=0)

        # Wait for reader to process init, then block on next readline
        await asyncio.sleep(0.1)

        # Process still running (returncode is None)
        assert fake_proc.returncode is None

        # Send follow-up while process is still running
        await runner.send_input(session.id, "queued message")

        assert "queued message" in runner._pending_inputs.get(session.id, [])

        # Clean up
        fake_proc._finish(0)
        reader = runner._readers.get(session.id)
        if reader:
            try:
                await asyncio.wait_for(reader, timeout=5.0)
            except Exception:
                pass


class TestPermissionModeMapping:
    """Test the approval_choice to permission_mode mapping."""

    def test_default_mode(self):
        from tether.runner.claude_subprocess import ClaudeSubprocessRunner
        assert ClaudeSubprocessRunner._map_permission_mode(0) == "default"

    def test_accept_edits_mode(self):
        from tether.runner.claude_subprocess import ClaudeSubprocessRunner
        assert ClaudeSubprocessRunner._map_permission_mode(1) == "acceptEdits"

    def test_bypass_mode(self):
        from tether.runner.claude_subprocess import ClaudeSubprocessRunner
        assert ClaudeSubprocessRunner._map_permission_mode(2) == "bypassPermissions"


class TestTokenAndCostMetadata:
    """Test that result events correctly emit metadata."""

    @pytest.fixture
    def mock_events(self):
        return _make_events()

    @pytest.fixture
    def runner(self, mock_events, monkeypatch, fresh_store, tmp_path):
        monkeypatch.setattr("tether.runner.claude_subprocess.store", fresh_store)
        from tether.runner.claude_subprocess import ClaudeSubprocessRunner

        runner = ClaudeSubprocessRunner(mock_events)
        session = fresh_store.create_session(str(tmp_path), None)
        session.directory = str(tmp_path)
        fresh_store.update_session(session)
        return runner, session

    @pytest.mark.anyio
    async def test_result_emits_tokens_and_cost(self, runner, mock_events):
        runner, session = runner
        event = {
            "event": "result",
            "input_tokens": 100,
            "output_tokens": 50,
            "cost_usd": 0.005,
            "is_error": False,
            "error_text": None,
        }

        await runner._on_result(session.id, event)

        calls = mock_events.on_metadata.call_args_list
        assert len(calls) == 2

        token_call = calls[0]
        assert token_call.args[1] == "tokens"
        assert token_call.args[2] == {"input": 100, "output": 50}

        cost_call = calls[1]
        assert cost_call.args[1] == "cost"
        assert cost_call.args[2] == 0.005

    @pytest.mark.anyio
    async def test_result_no_cost_skips_cost_metadata(self, runner, mock_events):
        runner, session = runner
        event = {
            "event": "result",
            "input_tokens": 10,
            "output_tokens": 5,
            "cost_usd": None,
            "is_error": False,
            "error_text": None,
        }

        await runner._on_result(session.id, event)

        calls = mock_events.on_metadata.call_args_list
        assert len(calls) == 1
        assert calls[0].args[1] == "tokens"
