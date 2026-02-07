"""Tests for the claude_local runner message handling."""

import sys
import pytest
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass


# Mock the SDK types since we don't want to import the actual SDK
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


# Create mock SDK module before importing the runner
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


class TestTextBlockCategorization:
    """Test that text blocks are correctly categorized as step vs final."""

    @pytest.fixture
    def mock_events(self):
        """Create a mock events handler that records calls."""
        events = MagicMock()
        events.on_output = AsyncMock()
        events.on_error = AsyncMock()
        return events

    @pytest.fixture
    def runner(self, mock_events, monkeypatch):
        """Create a ClaudeLocalRunner with mocked dependencies."""
        # Inject mock SDK into sys.modules before importing the runner
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", _mock_sdk)

        # Remove cached import if present so it reimports with mock
        sys.modules.pop("tether.runner.claude_local", None)

        from tether.runner.claude_local import ClaudeLocalRunner

        runner = ClaudeLocalRunner(mock_events)
        return runner

    @pytest.mark.anyio
    async def test_single_text_block_is_final(self, runner, mock_events):
        """A single text block without tool use should be final."""
        message = MockAssistantMessage(
            content=[MockTextBlock(text="Hello, how can I help?")]
        )

        await runner._handle_assistant_message("sess_1", message)

        mock_events.on_output.assert_called_once()
        call_args = mock_events.on_output.call_args
        assert call_args.kwargs["kind"] == "final"
        assert call_args.kwargs["is_final"] is True

    @pytest.mark.anyio
    async def test_text_with_tool_use_is_step(self, runner, mock_events):
        """Text followed by tool use should be step (intermediate)."""
        message = MockAssistantMessage(
            content=[
                MockTextBlock(text="Let me read that file for you."),
                MockToolUseBlock(name="Read"),
            ]
        )

        await runner._handle_assistant_message("sess_1", message)

        # Find the text output call
        text_calls = [
            c for c in mock_events.on_output.call_args_list
            if "Let me read" in str(c)
        ]
        assert len(text_calls) == 1
        call_args = text_calls[0]
        assert call_args.kwargs["kind"] == "step"
        assert call_args.kwargs["is_final"] is False

    @pytest.mark.anyio
    async def test_multiple_text_blocks_last_is_final(self, runner, mock_events):
        """With multiple text blocks and no tools, only last is final."""
        message = MockAssistantMessage(
            content=[
                MockTextBlock(text="First, let me think about this."),
                MockTextBlock(text="Here is my answer."),
            ]
        )

        await runner._handle_assistant_message("sess_1", message)

        calls = mock_events.on_output.call_args_list
        # First text block should be step
        first_call = [c for c in calls if "First" in str(c)][0]
        assert first_call.kwargs["kind"] == "step"
        assert first_call.kwargs["is_final"] is False

        # Last text block should be final
        last_call = [c for c in calls if "answer" in str(c)][0]
        assert last_call.kwargs["kind"] == "final"
        assert last_call.kwargs["is_final"] is True

    @pytest.mark.anyio
    async def test_multiple_text_with_tool_all_step(self, runner, mock_events):
        """With tool use, all text blocks should be step."""
        message = MockAssistantMessage(
            content=[
                MockTextBlock(text="I'll check the file."),
                MockToolUseBlock(name="Read"),
                MockTextBlock(text="Now I see the content."),
            ]
        )

        await runner._handle_assistant_message("sess_1", message)

        text_calls = [
            c for c in mock_events.on_output.call_args_list
            if c.args[2] and not c.args[2].startswith("[tool")
        ]
        # All text calls should be step
        for call in text_calls:
            assert call.kwargs["kind"] == "step"
            assert call.kwargs["is_final"] is False

    @pytest.mark.anyio
    async def test_tool_use_is_always_step(self, runner, mock_events):
        """Tool use blocks should always be step."""
        message = MockAssistantMessage(
            content=[MockToolUseBlock(name="Bash")]
        )

        await runner._handle_assistant_message("sess_1", message)

        mock_events.on_output.assert_called_once()
        call_args = mock_events.on_output.call_args
        assert call_args.kwargs["kind"] == "step"
        assert "[tool: Bash]" in call_args.args[2]

    @pytest.mark.anyio
    async def test_thinking_block_is_step(self, runner, mock_events):
        """Thinking blocks should be step."""
        message = MockAssistantMessage(
            content=[
                MockThinkingBlock(thinking="Let me consider this..."),
                MockTextBlock(text="Here's my answer."),
            ]
        )

        await runner._handle_assistant_message("sess_1", message)

        thinking_calls = [
            c for c in mock_events.on_output.call_args_list
            if "[thinking]" in str(c)
        ]
        assert len(thinking_calls) == 1
        assert thinking_calls[0].kwargs["kind"] == "step"

    @pytest.mark.anyio
    async def test_error_message_emits_error(self, runner, mock_events):
        """Error in message should emit error event."""
        message = MockAssistantMessage(
            content=[],
            error="rate_limit"
        )

        await runner._handle_assistant_message("sess_1", message)

        mock_events.on_error.assert_called_once()
        call_args = mock_events.on_error.call_args
        assert "RATE_LIMIT" in call_args.args[1]


class TestClaudeLocalTurnLifecycle:
    """Test that a claude_local turn completes and awaits input."""

    @pytest.fixture
    def mock_events(self):
        events = MagicMock()
        events.on_output = AsyncMock()
        events.on_error = AsyncMock()
        events.on_header = AsyncMock()
        events.on_metadata = AsyncMock()
        events.on_heartbeat = AsyncMock()
        events.on_awaiting_input = AsyncMock()
        return events

    @pytest.fixture
    def runner(self, mock_events, monkeypatch, fresh_store, tmp_path):
        # Inject mock SDK into sys.modules before importing the runner
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", _mock_sdk)
        sys.modules.pop("tether.runner.claude_local", None)

        from tether.runner.claude_local import ClaudeLocalRunner

        # Patch the runner's store to use the fresh store
        monkeypatch.setattr("tether.runner.claude_local.store", fresh_store)

        runner = ClaudeLocalRunner(mock_events)

        # Avoid background heartbeats
        async def _noop(*args, **kwargs):
            return None

        runner._heartbeat_loop = _noop  # type: ignore[assignment]

        # Create a session with a directory for cwd
        session = fresh_store.create_session(str(tmp_path), None)
        session.directory = str(tmp_path)
        fresh_store.update_session(session)

        return runner, session

    @pytest.mark.anyio
    async def test_result_message_ends_turn(self, runner, mock_events):
        runner, session = runner

        async def mock_query(prompt, options):
            # Consume the initial prompt without waiting for completion
            async for _ in prompt:
                break
            yield MockSystemMessage(subtype="init", data={"session_id": "sdk_1", "model": "claude"})
            yield MockAssistantMessage(content=[MockTextBlock(text="Hi")])
            yield MockResultMessage(usage={"input_tokens": 1, "output_tokens": 1})

        _mock_sdk.query = mock_query

        await runner.start(session.id, "hello", approval_choice=0)

        task = runner._tasks[session.id]
        await task

        assert task.done()
        mock_events.on_awaiting_input.assert_awaited_once()


class TestSessionRebinding:
    """Test that session binding stays consistent across restarts and mismatches."""

    @pytest.fixture
    def mock_events(self):
        events = MagicMock()
        events.on_output = AsyncMock()
        events.on_error = AsyncMock()
        events.on_header = AsyncMock()
        events.on_metadata = AsyncMock()
        events.on_heartbeat = AsyncMock()
        events.on_awaiting_input = AsyncMock()
        events.on_exit = AsyncMock()
        return events

    @pytest.fixture
    def runner(self, mock_events, monkeypatch, fresh_store, tmp_path):
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", _mock_sdk)
        sys.modules.pop("tether.runner.claude_local", None)

        from tether.runner.claude_local import ClaudeLocalRunner

        monkeypatch.setattr("tether.runner.claude_local.store", fresh_store)

        runner = ClaudeLocalRunner(mock_events)

        async def _noop(*args, **kwargs):
            return None

        runner._heartbeat_loop = _noop  # type: ignore[assignment]

        session = fresh_store.create_session(str(tmp_path), None)
        session.directory = str(tmp_path)
        fresh_store.update_session(session)

        return runner, session, fresh_store

    def _patch_query(self, monkeypatch, mock_query):
        """Patch query on the already-imported runner module."""
        monkeypatch.setattr("tether.runner.claude_local.query", mock_query)

    @pytest.mark.anyio
    async def test_first_run_binds_session(self, runner, mock_events, monkeypatch):
        """First run stores SDK session ID in both cache and store."""
        runner, session, store = runner

        async def mock_query(prompt, options):
            async for _ in prompt:
                break
            yield MockSystemMessage(subtype="init", data={"session_id": "sdk_aaa", "model": "claude"})
            yield MockResultMessage()

        self._patch_query(monkeypatch, mock_query)

        await runner.start(session.id, "hello", approval_choice=0)
        await runner._tasks[session.id]

        assert runner._sdk_sessions[session.id] == "sdk_aaa"
        assert store.get_runner_session_id(session.id) == "sdk_aaa"

    @pytest.mark.anyio
    async def test_resume_same_session_keeps_binding(self, runner, mock_events, monkeypatch):
        """Resuming with matching session ID keeps the binding unchanged."""
        runner, session, store = runner
        store.set_runner_session_id(session.id, "sdk_aaa")
        runner._sdk_sessions[session.id] = "sdk_aaa"

        async def mock_query(prompt, options):
            async for _ in prompt:
                break
            yield MockSystemMessage(subtype="init", data={"session_id": "sdk_aaa", "model": "claude"})
            yield MockResultMessage()

        self._patch_query(monkeypatch, mock_query)

        await runner.send_input(session.id, "follow-up")
        await runner._tasks[session.id]

        assert runner._sdk_sessions[session.id] == "sdk_aaa"
        assert store.get_runner_session_id(session.id) == "sdk_aaa"

    @pytest.mark.anyio
    async def test_mismatch_rebinds_to_new_session(self, runner, mock_events, monkeypatch):
        """When SDK returns different session, binding updates to the new one."""
        runner, session, store = runner
        store.set_runner_session_id(session.id, "sdk_aaa")
        runner._sdk_sessions[session.id] = "sdk_aaa"

        async def mock_query(prompt, options):
            async for _ in prompt:
                break
            yield MockSystemMessage(subtype="init", data={"session_id": "sdk_bbb", "model": "claude"})
            yield MockResultMessage()

        self._patch_query(monkeypatch, mock_query)

        await runner.send_input(session.id, "follow-up")
        await runner._tasks[session.id]

        # Both cache and store should reflect the new session
        assert runner._sdk_sessions[session.id] == "sdk_bbb"
        assert store.get_runner_session_id(session.id) == "sdk_bbb"

    @pytest.mark.anyio
    async def test_mismatch_after_restart_rebinds(self, runner, mock_events, monkeypatch):
        """Simulates agent restart: cache empty, store has old ID, SDK returns new."""
        runner, session, store = runner
        # Store has a binding but cache is empty (simulates restart)
        store.set_runner_session_id(session.id, "sdk_aaa")
        # _sdk_sessions is empty — runner was just created

        resumes_seen = []

        async def mock_query(prompt, options):
            resumes_seen.append(options.resume)
            async for _ in prompt:
                break
            yield MockSystemMessage(subtype="init", data={"session_id": "sdk_bbb", "model": "claude"})
            yield MockResultMessage()

        self._patch_query(monkeypatch, mock_query)

        await runner.send_input(session.id, "hello after restart")
        await runner._tasks[session.id]

        # Should have tried to resume the stored session
        assert resumes_seen == ["sdk_aaa"]
        # Both should now point to the new session
        assert runner._sdk_sessions[session.id] == "sdk_bbb"
        assert store.get_runner_session_id(session.id) == "sdk_bbb"

    @pytest.mark.anyio
    async def test_subsequent_resume_uses_rebound_session(self, runner, mock_events, monkeypatch):
        """After rebinding, next input resumes the new session, not the old one."""
        runner, session, store = runner
        store.set_runner_session_id(session.id, "sdk_aaa")
        runner._sdk_sessions[session.id] = "sdk_aaa"

        resumes_seen = []

        async def mock_query(prompt, options):
            resumes_seen.append(options.resume)
            async for _ in prompt:
                break
            sdk_id = "sdk_bbb"
            yield MockSystemMessage(subtype="init", data={"session_id": sdk_id, "model": "claude"})
            yield MockResultMessage()

        self._patch_query(monkeypatch, mock_query)

        # First call triggers rebind
        await runner.send_input(session.id, "first")
        await runner._tasks[session.id]

        # Second call should use the rebound ID
        await runner.send_input(session.id, "second")
        await runner._tasks[session.id]

        assert resumes_seen == ["sdk_aaa", "sdk_bbb"]


class TestPermissionModePersistence:
    """Test that permission mode survives runner restarts."""

    @pytest.fixture
    def mock_events(self):
        events = MagicMock()
        events.on_output = AsyncMock()
        events.on_error = AsyncMock()
        events.on_header = AsyncMock()
        events.on_metadata = AsyncMock()
        events.on_heartbeat = AsyncMock()
        events.on_awaiting_input = AsyncMock()
        events.on_exit = AsyncMock()
        return events

    @pytest.fixture
    def runner(self, mock_events, monkeypatch, fresh_store, tmp_path):
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", _mock_sdk)
        sys.modules.pop("tether.runner.claude_local", None)

        from tether.runner.claude_local import ClaudeLocalRunner

        monkeypatch.setattr("tether.runner.claude_local.store", fresh_store)

        runner = ClaudeLocalRunner(mock_events)

        async def _noop(*args, **kwargs):
            return None

        runner._heartbeat_loop = _noop  # type: ignore[assignment]

        session = fresh_store.create_session(str(tmp_path), None)
        session.directory = str(tmp_path)
        fresh_store.update_session(session)

        return runner, session, fresh_store

    def _patch_query(self, monkeypatch, mock_query):
        monkeypatch.setattr("tether.runner.claude_local.query", mock_query)

    @pytest.mark.anyio
    async def test_send_input_uses_persisted_approval_mode(self, runner, mock_events, monkeypatch):
        """After restart, send_input reads approval_mode from session, not default."""
        runner, session, store = runner

        # Persist interactive mode (0 = "default") on the session
        session.approval_mode = 0
        store.update_session(session)

        # Runner has empty _permission_modes cache (simulates restart)
        assert session.id not in runner._permission_modes

        permission_modes_seen = []

        async def mock_query(prompt, options):
            permission_modes_seen.append(options.permission_mode)
            async for _ in prompt:
                break
            yield MockSystemMessage(subtype="init", data={"session_id": "sdk_1", "model": "claude"})
            yield MockResultMessage()

        self._patch_query(monkeypatch, mock_query)

        await runner.send_input(session.id, "test input")
        await runner._tasks[session.id]

        # Should use "default" (interactive), NOT "bypassPermissions"
        assert permission_modes_seen == ["default"]

    @pytest.mark.anyio
    async def test_send_input_without_approval_mode_uses_interactive(self, runner, mock_events, monkeypatch):
        """Without persisted approval_mode, default to interactive (not bypass)."""
        runner, session, store = runner

        # No approval_mode set (None)
        assert session.approval_mode is None

        permission_modes_seen = []

        async def mock_query(prompt, options):
            permission_modes_seen.append(options.permission_mode)
            async for _ in prompt:
                break
            yield MockSystemMessage(subtype="init", data={"session_id": "sdk_1", "model": "claude"})
            yield MockResultMessage()

        self._patch_query(monkeypatch, mock_query)

        await runner.send_input(session.id, "test input")
        await runner._tasks[session.id]

        # approval_mode=None → _map_permission_mode(0) → "default" (interactive)
        assert permission_modes_seen == ["default"]
