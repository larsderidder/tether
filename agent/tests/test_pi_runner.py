"""Tests for the pi RPC runner adapter."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tether.runner.pi_rpc import PiRpcRunner, _find_pi_binary


class FakeRunnerEvents:
    """Fake RunnerEvents that records all calls."""

    def __init__(self) -> None:
        self.outputs: list[dict] = []
        self.errors: list[dict] = []
        self.headers: list[dict] = []
        self.heartbeats: list[dict] = []
        self.permissions: list[dict] = []
        self.permission_resolved: list[dict] = []
        self.awaiting_input_count = 0
        self.exit_count = 0

    async def on_output(self, session_id, stream, text, *, kind="final", is_final=None):
        self.outputs.append({
            "session_id": session_id,
            "stream": stream,
            "text": text,
            "kind": kind,
            "is_final": is_final,
        })

    async def on_error(self, session_id, code, message):
        self.errors.append({
            "session_id": session_id,
            "code": code,
            "message": message,
        })

    async def on_exit(self, session_id, exit_code):
        self.exit_count += 1

    async def on_awaiting_input(self, session_id):
        self.awaiting_input_count += 1

    async def on_metadata(self, session_id, key, value, raw):
        pass

    async def on_heartbeat(self, session_id, elapsed_s, done):
        self.heartbeats.append({"session_id": session_id, "done": done})

    async def on_header(self, session_id, *, title, model=None, provider=None, **kw):
        self.headers.append({
            "session_id": session_id,
            "title": title,
            "model": model,
            "provider": provider,
        })

    async def on_permission_request(self, session_id, request_id, tool_name, tool_input, suggestions=None):
        self.permissions.append({
            "session_id": session_id,
            "request_id": request_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
        })

    async def on_permission_resolved(self, session_id, request_id, resolved_by, allowed, message=None):
        self.permission_resolved.append({
            "session_id": session_id,
            "request_id": request_id,
            "resolved_by": resolved_by,
            "allowed": allowed,
        })


def test_find_pi_binary() -> None:
    """Verify _find_pi_binary returns a path or None without crashing."""
    result = _find_pi_binary()
    # On this machine pi should be installed
    assert result is None or "pi" in result


class TestPiRpcEventHandling:
    """Test event dispatch without spawning real subprocesses."""

    @pytest.fixture
    def runner_and_events(self):
        events = FakeRunnerEvents()
        runner = PiRpcRunner(events)
        return runner, events

    @pytest.mark.anyio
    async def test_handle_text_delta(self, runner_and_events):
        runner, events = runner_and_events
        proc = MagicMock()

        event = {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "delta": "Hello world",
            },
        }
        await runner._handle_event("sess1", proc, event)

        assert len(events.outputs) == 1
        assert events.outputs[0]["text"] == "Hello world"
        assert events.outputs[0]["kind"] == "step"
        assert events.outputs[0]["is_final"] is False

    @pytest.mark.anyio
    async def test_handle_thinking_delta(self, runner_and_events):
        runner, events = runner_and_events
        proc = MagicMock()

        event = {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "thinking_delta",
                "delta": "Let me consider...",
            },
        }
        await runner._handle_event("sess1", proc, event)

        assert len(events.outputs) == 1
        assert "[thinking]" in events.outputs[0]["text"]
        assert "Let me consider..." in events.outputs[0]["text"]

    @pytest.mark.anyio
    async def test_handle_tool_execution_start(self, runner_and_events, fresh_store):
        runner, events = runner_and_events
        proc = MagicMock()

        # Create a session in the store first
        session = fresh_store.create_session(repo_id="/tmp/test", base_ref=None)
        session_id = session.id

        event = {
            "type": "tool_execution_start",
            "toolCallId": "call_123",
            "toolName": "bash",
            "args": {"command": "ls -la"},
        }
        await runner._handle_event(session_id, proc, event)

        # Should emit output for the tool start
        assert any("[tool: bash]" in o["text"] for o in events.outputs)

        # Pi auto-approves tools, so no permission request should be emitted
        # Instead, should directly emit permission_resolved
        assert len(events.permissions) == 0
        assert len(events.permission_resolved) == 1
        assert events.permission_resolved[0]["allowed"] is True
        assert events.permission_resolved[0]["resolved_by"] == "auto"

    @pytest.mark.anyio
    async def test_handle_tool_execution_start_read_no_permission(self, runner_and_events):
        runner, events = runner_and_events
        proc = MagicMock()

        event = {
            "type": "tool_execution_start",
            "toolCallId": "call_456",
            "toolName": "read",
            "args": {"path": "test.txt"},
        }
        await runner._handle_event("sess1", proc, event)

        # read is NOT in _PERMISSION_TOOLS — no permission request
        assert len(events.permissions) == 0
        assert any("[tool: read]" in o["text"] for o in events.outputs)

    @pytest.mark.anyio
    async def test_handle_tool_execution_end(self, runner_and_events):
        runner, events = runner_and_events
        proc = MagicMock()

        event = {
            "type": "tool_execution_end",
            "toolCallId": "call_123",
            "toolName": "bash",
            "result": {
                "content": [{"type": "text", "text": "file1.txt\nfile2.txt"}],
                "details": {},
            },
            "isError": False,
        }
        await runner._handle_event("sess1", proc, event)

        assert len(events.outputs) == 1
        assert "[result]" in events.outputs[0]["text"]
        assert "file1.txt" in events.outputs[0]["text"]

    @pytest.mark.anyio
    async def test_handle_tool_execution_end_error(self, runner_and_events):
        runner, events = runner_and_events
        proc = MagicMock()

        event = {
            "type": "tool_execution_end",
            "toolCallId": "call_123",
            "toolName": "bash",
            "result": {
                "content": [{"type": "text", "text": "command not found"}],
                "details": {},
            },
            "isError": True,
        }
        await runner._handle_event("sess1", proc, event)

        assert len(events.outputs) == 1
        assert "[error]" in events.outputs[0]["text"]

    @pytest.mark.anyio
    async def test_handle_agent_start_end(self, runner_and_events):
        runner, events = runner_and_events
        proc = MagicMock()

        await runner._handle_event("sess1", proc, {"type": "agent_start"})
        assert runner._is_streaming.get("sess1") is True

        await runner._handle_event("sess1", proc, {
            "type": "agent_end",
            "messages": [{
                "role": "assistant",
                "content": [{"type": "text", "text": "Final answer"}],
            }],
        })
        assert runner._is_streaming.get("sess1") is False

        # Should have emitted the final text
        final_outputs = [o for o in events.outputs if o["is_final"] is True]
        assert len(final_outputs) == 1
        assert final_outputs[0]["text"] == "Final answer"

    @pytest.mark.anyio
    async def test_handle_get_state_response(self, runner_and_events, fresh_store):
        runner, events = runner_and_events
        proc = MagicMock()

        session = fresh_store.create_session(repo_id="/tmp/test", base_ref=None)

        event = {
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {
                "model": {
                    "id": "claude-sonnet-4-20250514",
                    "name": "Claude Sonnet 4",
                    "provider": "anthropic",
                },
                "sessionFile": f"/home/user/.pi/agent/sessions/--tmp-test--/2026-02-11_{session.id}.jsonl",
                "isStreaming": False,
            },
        }
        await runner._handle_event(session.id, proc, event)

        # Should emit updated header with model info
        assert any("Claude Sonnet 4" in h["title"] for h in events.headers)

    @pytest.mark.anyio
    async def test_handle_stream_error(self, runner_and_events):
        runner, events = runner_and_events
        proc = MagicMock()

        event = {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "error",
                "reason": "aborted",
            },
        }
        await runner._handle_event("sess1", proc, event)

        assert len(events.errors) == 1
        assert "aborted" in events.errors[0]["message"]

    @pytest.mark.anyio
    async def test_handle_auto_compaction(self, runner_and_events):
        runner, events = runner_and_events
        proc = MagicMock()

        await runner._handle_event("sess1", proc, {
            "type": "auto_compaction_start",
            "reason": "threshold",
        })
        assert any("compacting" in o["text"] for o in events.outputs)

        await runner._handle_event("sess1", proc, {
            "type": "auto_compaction_end",
            "result": {"tokensBefore": 150000},
        })
        assert any("150000" in o["text"] for o in events.outputs)

    @pytest.mark.anyio
    async def test_handle_notify_extension(self, runner_and_events):
        runner, events = runner_and_events
        proc = MagicMock()

        event = {
            "type": "extension_ui_request",
            "id": "uuid-1",
            "method": "notify",
            "message": "Extension loaded!",
        }
        await runner._handle_event("sess1", proc, event)

        assert any("Extension loaded!" in o["text"] for o in events.outputs)

    @pytest.mark.anyio
    async def test_handle_failed_prompt_response(self, runner_and_events):
        runner, events = runner_and_events
        proc = MagicMock()

        event = {
            "type": "response",
            "command": "prompt",
            "success": False,
            "error": "Agent is busy",
        }
        await runner._handle_event("sess1", proc, event)

        assert len(events.errors) == 1
        assert "Agent is busy" in events.errors[0]["message"]
