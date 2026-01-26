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


# Create mock SDK module before importing the runner
_mock_sdk = MagicMock()
_mock_sdk.TextBlock = MockTextBlock
_mock_sdk.ToolUseBlock = MockToolUseBlock
_mock_sdk.ToolResultBlock = MockToolResultBlock
_mock_sdk.ThinkingBlock = MockThinkingBlock
_mock_sdk.AssistantMessage = MockAssistantMessage
_mock_sdk.SystemMessage = MagicMock
_mock_sdk.ResultMessage = MagicMock
_mock_sdk.ClaudeAgentOptions = MagicMock
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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
