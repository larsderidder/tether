"""Tests for claude_sdk_worker (child-side subprocess logic)."""

import asyncio
import json
import sys

import pytest
from unittest.mock import MagicMock
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Mock SDK types
# ---------------------------------------------------------------------------


@dataclass
class MockTextBlock:
    text: str
    type: str = "text"


@dataclass
class MockToolUseBlock:
    name: str
    id: str = "tool_1"
    input: dict = None
    type: str = "tool_use"

    def __post_init__(self):
        if self.input is None:
            self.input = {}


@dataclass
class MockToolResultBlock:
    content: str
    is_error: bool = False
    type: str = "tool_result"


@dataclass
class MockThinkingBlock:
    thinking: str
    type: str = "thinking"


@dataclass
class MockAssistantMessage:
    content: list
    error: str = None


@dataclass
class MockSystemMessage:
    subtype: str
    data: dict


@dataclass
class MockResultMessage:
    usage: dict | None = None
    total_cost_usd: float | None = None
    is_error: bool = False
    result: str | None = None


_mock_sdk = MagicMock()
_mock_sdk.TextBlock = MockTextBlock
_mock_sdk.ToolUseBlock = MockToolUseBlock
_mock_sdk.ToolResultBlock = MockToolResultBlock
_mock_sdk.ThinkingBlock = MockThinkingBlock
_mock_sdk.AssistantMessage = MockAssistantMessage
_mock_sdk.SystemMessage = MockSystemMessage
_mock_sdk.ResultMessage = MockResultMessage
_mock_sdk.ClaudeAgentOptions = MagicMock
_mock_sdk.HookMatcher = MagicMock
_mock_sdk.query = MagicMock()


class TestBlockSerialization:
    """Test that _serialize_blocks produces correct dicts."""

    @pytest.fixture(autouse=True)
    def inject_mock_sdk(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", _mock_sdk)
        sys.modules.pop("tether.runner.claude_sdk_worker", None)

    def test_text_block(self):
        from tether.runner.claude_sdk_worker import _serialize_blocks

        result = _serialize_blocks([MockTextBlock(text="hello")])
        assert result == [{"type": "text", "text": "hello"}]

    def test_tool_use_block(self):
        from tether.runner.claude_sdk_worker import _serialize_blocks

        result = _serialize_blocks([MockToolUseBlock(name="Bash", id="t1", input={"command": "ls"})])
        assert result == [{"type": "tool_use", "name": "Bash", "id": "t1", "input": {"command": "ls"}}]

    def test_tool_result_block(self):
        from tether.runner.claude_sdk_worker import _serialize_blocks

        result = _serialize_blocks([MockToolResultBlock(content="ok", is_error=False)])
        assert result == [{"type": "tool_result", "content": "ok", "is_error": False}]

    def test_tool_result_error(self):
        from tether.runner.claude_sdk_worker import _serialize_blocks

        result = _serialize_blocks([MockToolResultBlock(content="fail", is_error=True)])
        assert result == [{"type": "tool_result", "content": "fail", "is_error": True}]

    def test_thinking_block(self):
        from tether.runner.claude_sdk_worker import _serialize_blocks

        result = _serialize_blocks([MockThinkingBlock(thinking="hmm")])
        assert result == [{"type": "thinking", "thinking": "hmm"}]

    def test_empty_thinking_skipped(self):
        from tether.runner.claude_sdk_worker import _serialize_blocks

        result = _serialize_blocks([MockThinkingBlock(thinking="")])
        assert result == []

    def test_mixed_blocks(self):
        from tether.runner.claude_sdk_worker import _serialize_blocks

        blocks = [
            MockThinkingBlock(thinking="let me think"),
            MockTextBlock(text="I'll check that file"),
            MockToolUseBlock(name="Read", id="t1", input={"path": "/foo"}),
            MockToolResultBlock(content="file contents", is_error=False),
            MockTextBlock(text="Here's what I found"),
        ]
        result = _serialize_blocks(blocks)
        assert len(result) == 5
        assert result[0]["type"] == "thinking"
        assert result[1]["type"] == "text"
        assert result[2]["type"] == "tool_use"
        assert result[3]["type"] == "tool_result"
        assert result[4]["type"] == "text"


class TestHandleMessage:
    """Test that _handle_message produces correct JSON events."""

    @pytest.fixture
    def worker_module(self, monkeypatch):
        """Import a fresh worker module with mock SDK and captured events.

        Returns (module, captured_events_list).
        """
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", _mock_sdk)
        # Clear both sys.modules entry AND parent package attribute to
        # guarantee a single fresh module object everywhere.
        sys.modules.pop("tether.runner.claude_sdk_worker", None)
        runner_pkg = sys.modules.get("tether.runner")
        if runner_pkg and hasattr(runner_pkg, "claude_sdk_worker"):
            delattr(runner_pkg, "claude_sdk_worker")

        import importlib
        mod = importlib.import_module("tether.runner.claude_sdk_worker")

        captured: list[dict] = []
        monkeypatch.setattr(mod, "_write_event", lambda event: captured.append(event))
        return mod, captured

    def test_init_message(self, worker_module):
        mod, captured = worker_module

        msg = MockSystemMessage(
            subtype="init",
            data={"session_id": "sdk_123", "model": "claude-opus", "claude_code_version": "2.0"},
        )
        mod._handle_message(msg)

        assert len(captured) == 1
        event = captured[0]
        assert event["event"] == "init"
        assert event["session_id"] == "sdk_123"
        assert event["model"] == "claude-opus"
        assert event["version"] == "2.0"

    def test_assistant_message_with_text(self, worker_module):
        mod, captured = worker_module

        msg = MockAssistantMessage(content=[MockTextBlock(text="Hello!")])
        mod._handle_message(msg)

        assert len(captured) == 1
        event = captured[0]
        assert event["event"] == "output"
        assert event["blocks"] == [{"type": "text", "text": "Hello!"}]

    def test_assistant_error(self, worker_module):
        mod, captured = worker_module

        msg = MockAssistantMessage(content=[], error="rate_limit")
        mod._handle_message(msg)

        assert len(captured) == 1
        event = captured[0]
        assert event["event"] == "error"
        assert event["code"] == "ASSISTANT_ERROR"

    def test_result_message(self, worker_module):
        mod, captured = worker_module

        msg = MockResultMessage(
            usage={"input_tokens": 100, "output_tokens": 50},
            total_cost_usd=0.005,
            is_error=False,
        )
        mod._handle_message(msg)

        assert len(captured) == 1
        event = captured[0]
        assert event["event"] == "result"
        assert event["input_tokens"] == 100
        assert event["output_tokens"] == 50
        assert event["cost_usd"] == 0.005
        assert event["is_error"] is False

    def test_result_error(self, worker_module):
        mod, captured = worker_module

        msg = MockResultMessage(
            usage={"input_tokens": 0, "output_tokens": 0},
            is_error=True,
            result="Something went wrong",
        )
        mod._handle_message(msg)

        event = captured[0]
        assert event["is_error"] is True
        assert event["error_text"] == "Something went wrong"

    def test_non_init_system_message_ignored(self, worker_module):
        mod, captured = worker_module

        msg = MockSystemMessage(subtype="other", data={})
        mod._handle_message(msg)

        assert len(captured) == 0


class TestQueryStreamCleanup:
    """Test that _run() suppresses cleanup errors from the SDK query stream."""

    @pytest.fixture
    def worker_module(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", _mock_sdk)
        sys.modules.pop("tether.runner.claude_sdk_worker", None)
        runner_pkg = sys.modules.get("tether.runner")
        if runner_pkg and hasattr(runner_pkg, "claude_sdk_worker"):
            delattr(runner_pkg, "claude_sdk_worker")
        import importlib
        mod = importlib.import_module("tether.runner.claude_sdk_worker")
        captured: list[dict] = []
        monkeypatch.setattr(mod, "_write_event", lambda event: captured.append(event))
        return mod, captured

    def _patch_stdin(self, mod, monkeypatch):
        """Patch _StdinReader to avoid real stdin pipe usage."""

        async def noop_start(self):
            self._loop = asyncio.get_running_loop()
            self._reader = asyncio.StreamReader()

        async def noop_readline(self):
            await asyncio.Event().wait()

        monkeypatch.setattr(mod._StdinReader, "start", noop_start)
        monkeypatch.setattr(mod._StdinReader, "readline", noop_readline)

    def _make_start_cmd(self):
        return {
            "cmd": "start",
            "prompt": "test",
            "cwd": None,
            "permission_mode": "bypassPermissions",
            "resume": None,
            "system_prompt": "test",
        }

    @pytest.mark.anyio
    async def test_runtime_error_during_aclose_suppressed(self, worker_module, monkeypatch):
        """RuntimeError from SDK cancel scope cleanup doesn't propagate."""
        mod, captured = worker_module
        self._patch_stdin(mod, monkeypatch)

        async def mock_query(prompt, options):
            try:
                yield MockSystemMessage(
                    subtype="init",
                    data={"session_id": "sdk_test", "model": "claude", "claude_code_version": "1.0"},
                )
                yield MockResultMessage(
                    usage={"input_tokens": 10, "output_tokens": 5},
                    total_cost_usd=0.001,
                )
            except GeneratorExit:
                raise RuntimeError(
                    "Attempted to exit cancel scope in a different task"
                )

        monkeypatch.setattr(_mock_sdk, "query", mock_query)
        await mod._run(self._make_start_cmd())

        events = [e["event"] for e in captured]
        assert "init" in events
        assert "result" in events

    @pytest.mark.anyio
    async def test_cancelled_error_during_aclose_suppressed(self, worker_module, monkeypatch):
        """CancelledError from SDK internal task group doesn't propagate."""
        mod, captured = worker_module
        self._patch_stdin(mod, monkeypatch)

        async def mock_query(prompt, options):
            try:
                yield MockSystemMessage(
                    subtype="init",
                    data={"session_id": "sdk_test", "model": "claude", "claude_code_version": "1.0"},
                )
                yield MockResultMessage(
                    usage={"input_tokens": 10, "output_tokens": 5},
                    total_cost_usd=0.001,
                )
            except GeneratorExit:
                raise asyncio.CancelledError()

        monkeypatch.setattr(_mock_sdk, "query", mock_query)
        await mod._run(self._make_start_cmd())

        events = [e["event"] for e in captured]
        assert "init" in events
        assert "result" in events


class TestMainShutdown:
    """Test that main() suppresses errors during event loop shutdown."""

    @pytest.fixture
    def worker_module(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", _mock_sdk)
        sys.modules.pop("tether.runner.claude_sdk_worker", None)
        runner_pkg = sys.modules.get("tether.runner")
        if runner_pkg and hasattr(runner_pkg, "claude_sdk_worker"):
            delattr(runner_pkg, "claude_sdk_worker")
        import importlib
        return importlib.import_module("tether.runner.claude_sdk_worker")

    def test_dangling_tasks_suppressed_during_shutdown(self, worker_module, monkeypatch):
        """Dangling tasks that raise on cancel don't crash main()."""
        mod = worker_module
        monkeypatch.setattr(mod, "_read_line_sync", lambda: {
            "cmd": "start",
            "prompt": "test",
            "cwd": None,
            "permission_mode": "bypassPermissions",
            "resume": None,
            "system_prompt": "test",
        })

        async def _run_with_dangling_task(start_cmd):
            async def _exploding():
                try:
                    await asyncio.sleep(999)
                except asyncio.CancelledError:
                    raise RuntimeError("cancel scope in different task")

            asyncio.create_task(_exploding())

        monkeypatch.setattr(mod, "_run", _run_with_dangling_task)
        mod.main()
