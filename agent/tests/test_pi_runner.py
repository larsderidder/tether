"""Tests for the pi RPC runner adapter."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from tether.models import SessionState
from tether.runner.base import RunnerUnavailableError
from tether.runner.pi_rpc import (
    PiRpcRunner,
    _PI_RPC_STREAM_LIMIT_BYTES,
    _TOOL_OUTPUT_MAX_CHARS,
    _TOOL_OUTPUT_MAX_LINES,
    _find_pi_binary,
)


class FakeStdin:
    """Fake async subprocess stdin that records JSON lines."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        pass


class FakeProcess:
    """Fake subprocess with writable stdin."""

    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.returncode = None


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

    async def on_output(
        self,
        session_id,
        stream,
        text,
        *,
        kind="final",
        is_final=None,
        bridge_segments=None,
    ):
        self.outputs.append(
            {
                "session_id": session_id,
                "stream": stream,
                "text": text,
                "kind": kind,
                "is_final": is_final,
                "bridge_segments": bridge_segments,
            }
        )

    async def on_error(self, session_id, code, message):
        self.errors.append(
            {
                "session_id": session_id,
                "code": code,
                "message": message,
            }
        )

    async def on_exit(self, session_id, exit_code):
        self.exit_count += 1

    async def on_awaiting_input(self, session_id):
        self.awaiting_input_count += 1

    async def on_metadata(self, session_id, key, value, raw):
        pass

    async def on_heartbeat(self, session_id, elapsed_s, done):
        self.heartbeats.append({"session_id": session_id, "done": done})

    async def on_header(self, session_id, *, title, model=None, provider=None, **kw):
        self.headers.append(
            {
                "session_id": session_id,
                "title": title,
                "model": model,
                "provider": provider,
            }
        )

    async def on_permission_request(
        self, session_id, request_id, tool_name, tool_input, suggestions=None
    ):
        self.permissions.append(
            {
                "session_id": session_id,
                "request_id": request_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            }
        )

    async def on_permission_resolved(
        self, session_id, request_id, resolved_by, allowed, message=None
    ):
        self.permission_resolved.append(
            {
                "session_id": session_id,
                "request_id": request_id,
                "resolved_by": resolved_by,
                "allowed": allowed,
            }
        )


def test_find_pi_binary() -> None:
    """Verify _find_pi_binary returns a path or None without crashing."""
    result = _find_pi_binary()
    # On this machine pi should be installed
    assert result is None or "pi" in result


@pytest.mark.anyio
async def test_send_prompt_includes_images() -> None:
    """Pi RPC prompts include validated image payloads."""

    runner = PiRpcRunner(FakeRunnerEvents())
    proc = FakeProcess()
    runner._processes["sess1"] = proc
    images = [{"type": "image", "data": "abc", "mimeType": "image/png"}]

    await runner._send_prompt("sess1", "describe this", images=images)

    payload = json.loads(proc.stdin.writes[0].decode())
    assert payload == {
        "type": "prompt",
        "message": "describe this",
        "images": images,
    }


@pytest.mark.anyio
async def test_spawn_uses_large_stream_limit(monkeypatch) -> None:
    """Pi RPC stdout can carry large JSON lines without reader overrun."""

    runner = PiRpcRunner(FakeRunnerEvents())
    runner._pi_binary = "/bin/echo"
    proc = MagicMock()
    proc.stdin = FakeStdin()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    proc.returncode = None
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    await runner._spawn("sess1", "/tmp", None)

    assert captured["limit"] == _PI_RPC_STREAM_LIMIT_BYTES
    assert captured["limit"] >= 100 * 1024 * 1024
    runner._cleanup("sess1")


@pytest.mark.anyio
async def test_spawn_fresh_session_is_persistent(monkeypatch) -> None:
    """Fresh pi RPC sessions should persist so they survive Tether restarts."""

    runner = PiRpcRunner(FakeRunnerEvents())
    runner._pi_binary = "/bin/echo"
    proc = MagicMock()
    proc.stdin = FakeStdin()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    proc.returncode = None
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    await runner._spawn("sess1", "/tmp", None)

    assert "--no-session" not in captured["args"]
    assert captured["args"] == ("/bin/echo", "--mode", "rpc")
    runner._cleanup("sess1")


@pytest.mark.anyio
async def test_resolve_session_file_blocks_huge_pi_history(
    tmp_path, monkeypatch, fresh_store
) -> None:
    """Huge pi session histories are not silently replaced with fresh context."""

    monkeypatch.setattr("tether.runner.pi_rpc.store", fresh_store)
    monkeypatch.setenv("TETHER_PI_RESUME_MAX_SESSION_FILE_BYTES", "10485760")
    runner = PiRpcRunner(FakeRunnerEvents())
    session = fresh_store.create_session(repo_id="/tmp/test", base_ref=None)
    session_file = tmp_path / "session.jsonl"
    session_file.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    fresh_store.set_runner_session_id(session.id, "pi-session-id")
    monkeypatch.setattr(
        "tether.runner.pi_rpc._find_session_file", lambda runner_sid: session_file
    )

    with pytest.raises(RunnerUnavailableError) as exc_info:
        await runner._resolve_session_file(session.id)

    assert "too large to resume" in str(exc_info.value)
    assert fresh_store.get_runner_session_id(session.id) == "pi-session-id"
    assert runner._session_files[session.id] == str(session_file)


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
        assert events.outputs[0]["bridge_segments"] == [
            {"kind": "assistant", "text": "Hello world"}
        ]

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
        assert events.outputs[0]["bridge_segments"] == [
            {"kind": "thinking", "text": "Let me consider..."}
        ]

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
        assert events.outputs[0]["bridge_segments"] == [
            {"kind": "tool_call", "text": '{"command": "ls -la"}', "label": "bash"}
        ]

        # Pi auto-approves tools, so no permission request should be emitted
        # Instead, should directly emit permission_resolved
        assert len(events.permissions) == 0
        assert len(events.permission_resolved) == 1
        assert events.permission_resolved[0]["allowed"] is True
        assert events.permission_resolved[0]["resolved_by"] == "auto"

    @pytest.mark.anyio
    async def test_handle_tool_execution_start_read_no_permission(
        self, runner_and_events
    ):
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
        assert events.outputs[0]["bridge_segments"] == [
            {"kind": "tool_result", "text": "file1.txt\nfile2.txt", "label": "bash"}
        ]

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
        assert events.outputs[0]["bridge_segments"] == [
            {"kind": "tool_error", "text": "command not found", "label": "bash"}
        ]

    @pytest.mark.anyio
    async def test_handle_tool_execution_update_truncates_large_output(
        self, runner_and_events
    ):
        runner, events = runner_and_events
        proc = MagicMock()
        text = "A" * (_TOOL_OUTPUT_MAX_CHARS + 250)

        event = {
            "type": "tool_execution_update",
            "toolCallId": "call_789",
            "toolName": "read",
            "partialResult": {
                "content": [{"type": "text", "text": text}],
            },
        }
        await runner._handle_event("sess1", proc, event)

        assert len(events.outputs) == 1
        assert "[read]" in events.outputs[0]["text"]
        assert "[truncated, additional characters omitted]" in events.outputs[0]["text"]
        assert len(events.outputs[0]["text"]) < len(text)

    @pytest.mark.anyio
    async def test_auto_retry_failure_after_final_is_status_only(
        self, runner_and_events, monkeypatch
    ):
        runner, events = runner_and_events
        proc = MagicMock()
        session = MagicMock()
        session.state = SessionState.AWAITING_INPUT
        monkeypatch.setattr(
            "tether.runner.pi_rpc.store.get_session", lambda session_id: session
        )

        runner._is_streaming["sess1"] = True
        runner._streamed_text["sess1"] = True

        await runner._handle_event(
            "sess1",
            proc,
            {
                "type": "auto_retry_end",
                "success": False,
                "finalError": "Codex SSE response headers timed out after 10000ms",
            },
        )

        assert "sess1" not in runner._is_streaming
        assert "sess1" not in runner._streamed_text
        assert events.errors == []
        assert events.outputs[0]["text"] == (
            "[notify] Retry failed: Codex SSE response headers timed out after 10000ms\n"
        )
        assert events.outputs[0]["bridge_segments"] == [
            {
                "kind": "status",
                "text": "Retry failed: Codex SSE response headers timed out after 10000ms",
            }
        ]

    @pytest.mark.anyio
    async def test_auto_retry_failure_before_final_marks_error(
        self, runner_and_events, monkeypatch
    ):
        runner, events = runner_and_events
        proc = MagicMock()
        session = MagicMock()
        session.state = SessionState.RUNNING
        monkeypatch.setattr(
            "tether.runner.pi_rpc.store.get_session", lambda session_id: session
        )

        await runner._handle_event(
            "sess1",
            proc,
            {
                "type": "auto_retry_end",
                "success": False,
                "finalError": "Codex SSE response headers timed out after 10000ms",
            },
        )

        assert events.errors == [
            {
                "session_id": "sess1",
                "code": "PI_RETRY_FAILED",
                "message": "Retry failed: Codex SSE response headers timed out after 10000ms",
            }
        ]

    @pytest.mark.anyio
    async def test_handle_tool_execution_end_truncates_large_output(
        self, runner_and_events
    ):
        runner, events = runner_and_events
        proc = MagicMock()
        text = "B" * (_TOOL_OUTPUT_MAX_CHARS + 125)

        event = {
            "type": "tool_execution_end",
            "toolCallId": "call_999",
            "toolName": "bash",
            "result": {
                "content": [{"type": "text", "text": text}],
                "details": {},
            },
            "isError": False,
        }
        await runner._handle_event("sess1", proc, event)

        assert len(events.outputs) == 1
        assert "[result]" in events.outputs[0]["text"]
        assert "[truncated, additional characters omitted]" in events.outputs[0]["text"]
        assert len(events.outputs[0]["text"]) < len(text)

    @pytest.mark.anyio
    async def test_handle_tool_execution_end_uses_configured_truncation(
        self, runner_and_events, monkeypatch
    ):
        monkeypatch.setenv("TETHER_PI_TOOL_OUTPUT_MAX_LINES", "5")
        monkeypatch.setenv("TETHER_PI_TOOL_OUTPUT_MAX_CHARS", "200")
        runner, events = runner_and_events
        proc = MagicMock()
        text = "\n".join(f"line {index}" for index in range(10))

        event = {
            "type": "tool_execution_end",
            "toolCallId": "call_configured",
            "toolName": "bash",
            "result": {
                "content": [{"type": "text", "text": text}],
                "details": {},
            },
            "isError": False,
        }
        await runner._handle_event("sess1", proc, event)

        assert "line 4" in events.outputs[0]["text"]
        assert "line 5" not in events.outputs[0]["text"]
        assert "[truncated, 5 more lines omitted]" in events.outputs[0]["text"]

    @pytest.mark.anyio
    async def test_handle_tool_execution_end_truncates_by_lines(
        self, runner_and_events
    ):
        runner, events = runner_and_events
        proc = MagicMock()
        text = "\n".join(f"line {index}" for index in range(_TOOL_OUTPUT_MAX_LINES + 3))

        event = {
            "type": "tool_execution_end",
            "toolCallId": "call_lines",
            "toolName": "bash",
            "result": {
                "content": [{"type": "text", "text": text}],
                "details": {},
            },
            "isError": False,
        }
        await runner._handle_event("sess1", proc, event)

        assert f"line {_TOOL_OUTPUT_MAX_LINES - 1}" in events.outputs[0]["text"]
        assert f"line {_TOOL_OUTPUT_MAX_LINES}" not in events.outputs[0]["text"]
        assert "[truncated, 3 more lines omitted]" in events.outputs[0]["text"]

    @pytest.mark.anyio
    async def test_handle_agent_start_end(self, runner_and_events):
        runner, events = runner_and_events
        proc = MagicMock()

        await runner._handle_event("sess1", proc, {"type": "agent_start"})
        assert runner._is_streaming.get("sess1") is True

        await runner._handle_event(
            "sess1",
            proc,
            {
                "type": "agent_end",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Final answer"}],
                    }
                ],
            },
        )
        assert not runner._is_streaming.get("sess1")

        # Should have emitted the final text
        final_outputs = [o for o in events.outputs if o["is_final"] is True]
        assert len(final_outputs) == 1
        assert final_outputs[0]["text"] == "Final answer"

    @pytest.mark.anyio
    async def test_agent_end_emits_clean_final_after_streaming_tokens(
        self, runner_and_events
    ):
        """Pi's final accumulated text should replace broken streamed prose."""
        runner, events = runner_and_events
        proc = MagicMock()

        await runner._handle_event("sess1", proc, {"type": "agent_start"})
        await runner._handle_event(
            "sess1",
            proc,
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "-350out-dir",
                },
            },
        )
        await runner._handle_event(
            "sess1",
            proc,
            {
                "type": "agent_end",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": "- 350 output cards\n- Clean final text",
                            }
                        ],
                    }
                ],
            },
        )

        assert [output["is_final"] for output in events.outputs] == [False, True]
        assert events.outputs[-1]["text"] == "- 350 output cards\n- Clean final text"
        assert events.awaiting_input_count == 1

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

        await runner._handle_event(
            "sess1",
            proc,
            {
                "type": "auto_compaction_start",
                "reason": "threshold",
            },
        )
        assert any("compacting" in o["text"] for o in events.outputs)

        await runner._handle_event(
            "sess1",
            proc,
            {
                "type": "auto_compaction_end",
                "result": {"tokensBefore": 150000},
            },
        )
        assert any("150000" in o["text"] for o in events.outputs)

    @pytest.mark.anyio
    async def test_handle_documented_compaction_events(self, runner_and_events):
        runner, events = runner_and_events
        proc = MagicMock()

        await runner._handle_event("sess1", proc, {"type": "compaction_start"})
        await runner._handle_event(
            "sess1",
            proc,
            {"type": "compaction_end", "result": {"tokensBefore": 42}},
        )

        assert any("compacting" in o["text"] for o in events.outputs)
        assert any("42" in o["text"] for o in events.outputs)

    @pytest.mark.anyio
    async def test_compact_writes_rpc_command(self, runner_and_events):
        runner, _events = runner_and_events
        proc = FakeProcess()
        runner._processes["sess1"] = proc

        await runner.compact("sess1", "focus on decisions")

        payload = json.loads(proc.stdin.writes[0].decode())
        assert payload == {
            "type": "compact",
            "customInstructions": "focus on decisions",
        }

    @pytest.mark.anyio
    async def test_handle_confirm_extension_round_trips_response(
        self, runner_and_events
    ):
        runner, events = runner_and_events
        proc = FakeProcess()
        runner._processes["sess1"] = proc

        await runner._handle_event(
            "sess1",
            proc,
            {
                "type": "extension_ui_request",
                "id": "uuid-1",
                "method": "confirm",
                "title": "Remember this?",
                "message": "Save this memory?",
            },
        )

        assert events.permissions[0]["request_id"] == "pi_extui:confirm:uuid-1"
        assert events.permissions[0]["tool_name"] == "AskUserQuestion"

        from tether.store import store

        assert store.resolve_pending_permission(
            "sess1",
            "pi_extui:confirm:uuid-1",
            {"behavior": "allow", "updated_input": {"value": "Yes"}},
        )
        await asyncio.sleep(0.05)

        payload = json.loads(proc.stdin.writes[0].decode())
        assert payload == {
            "type": "extension_ui_response",
            "id": "uuid-1",
            "confirmed": True,
        }

    @pytest.mark.anyio
    async def test_handle_input_extension_round_trips_value(self, runner_and_events):
        runner, events = runner_and_events
        proc = FakeProcess()
        runner._processes["sess1"] = proc

        await runner._handle_event(
            "sess1",
            proc,
            {
                "type": "extension_ui_request",
                "id": "uuid-2",
                "method": "input",
                "title": "Need value",
                "placeholder": "Type value",
            },
        )

        assert events.permissions[0]["request_id"] == "pi_extui:input:uuid-2"
        assert events.permissions[0]["tool_name"] == "Input needed"

        from tether.store import store

        assert store.resolve_pending_permission(
            "sess1",
            "pi_extui:input:uuid-2",
            {"behavior": "allow", "updated_input": {"value": "manual answer"}},
        )
        await asyncio.sleep(0.05)

        payload = json.loads(proc.stdin.writes[0].decode())
        assert payload == {
            "type": "extension_ui_response",
            "id": "uuid-2",
            "value": "manual answer",
        }

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
    async def test_text_delta_after_toolish_output_gets_assistant_marker(
        self, runner_and_events
    ):
        runner, events = runner_and_events
        proc = MagicMock()

        await runner._handle_event(
            "sess1",
            proc,
            {
                "type": "extension_ui_request",
                "id": "uuid-1",
                "method": "notify",
                "message": "Extension loaded!",
            },
        )
        await runner._handle_event(
            "sess1",
            proc,
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "Perfect. Ready when you are.",
                },
            },
        )

        assert events.outputs[-1]["text"] == "[assistant] Perfect. Ready when you are."

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

    @pytest.mark.anyio
    async def test_handle_agent_end_with_escape_sequences(
        self, runner_and_events, fresh_store
    ):
        """Test that terminal escape sequences are stripped from agent_end events."""
        runner, events = runner_and_events
        proc = MagicMock()

        # Create a session first
        session = fresh_store.create_session(repo_id="/tmp/test", base_ref=None)
        session_id = session.id

        # Simulate the actual output from pi with OSC notification escape sequence
        # This is what pi emits: ]777;notify;π;Hey!{"type":"agent_end",...}
        raw_line = ']777;notify;π;Ready!{"type":"agent_end","messages":[{"role":"assistant","content":[{"type":"text","text":"Ready!"}]}]}'

        # The reader should strip the escape sequence and parse the JSON
        # Simulate what _read_events does
        line = raw_line
        if "]777;notify;" in line:
            json_start = line.find('{"type":')
            if json_start > 0:
                line = line[json_start:]

        event = json.loads(line)
        await runner._handle_event(session_id, proc, event)

        # Should emit final output from agent_end
        final_outputs = [o for o in events.outputs if o.get("is_final") is True]
        assert len(final_outputs) == 1
        assert final_outputs[0]["text"] == "Ready!"
        assert final_outputs[0]["kind"] == "final"
