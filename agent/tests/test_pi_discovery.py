"""Tests for pi coding agent session discovery and parsing."""

from __future__ import annotations

import json
from pathlib import Path

from tether.discovery.pi_sessions import (
    list_pi_sessions,
    get_pi_session_detail,
    _decode_directory_name,
    _encode_directory_name,
)


def _write_pi_session(path: Path, session_id: str, cwd: str = "/home/lars/project") -> None:
    """Write a minimal pi session JSONL file."""
    records = [
        {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": "2026-02-11T08:00:00.000Z",
            "cwd": cwd,
        },
        {
            "type": "model_change",
            "id": "model1",
            "parentId": None,
            "timestamp": "2026-02-11T08:00:00.001Z",
            "provider": "anthropic",
            "modelId": "claude-sonnet-4-20250514",
        },
        {
            "type": "message",
            "id": "msg1",
            "parentId": "model1",
            "timestamp": "2026-02-11T08:01:00.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Hello pi"}],
                "timestamp": 1770797760000,
            },
        },
        {
            "type": "message",
            "id": "msg2",
            "parentId": "msg1",
            "timestamp": "2026-02-11T08:01:10.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "User is greeting me"},
                    {"type": "text", "text": "Hello! How can I help?"},
                ],
                "api": "anthropic-messages",
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
                "usage": {
                    "input": 100,
                    "output": 50,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                },
                "stopReason": "stop",
                "timestamp": 1770797760010,
            },
        },
        {
            "type": "message",
            "id": "msg3",
            "parentId": "msg2",
            "timestamp": "2026-02-11T08:02:00.000Z",
            "message": {
                "role": "user",
                "content": "What files are here?",
                "timestamp": 1770797820000,
            },
        },
        {
            "type": "message",
            "id": "msg4",
            "parentId": "msg3",
            "timestamp": "2026-02-11T08:02:15.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check..."},
                    {
                        "type": "toolCall",
                        "id": "call_1",
                        "name": "bash",
                        "arguments": {"command": "ls"},
                    },
                ],
                "api": "anthropic-messages",
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
                "stopReason": "toolUse",
                "timestamp": 1770797835000,
            },
        },
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def test_decode_directory_name() -> None:
    assert _decode_directory_name("--home-lars-project--") == "/home/lars/project"
    assert _decode_directory_name("--home-lars-workspace--") == "/home/lars/workspace"


def test_encode_directory_name() -> None:
    assert _encode_directory_name("/home/lars/project") == "--home-lars-project--"
    assert _encode_directory_name("/home/lars/workspace") == "--home-lars-workspace--"


def test_roundtrip_directory_encoding() -> None:
    paths = ["/home/lars/project", "/home/lars/workspace", "/tmp/test"]
    for path in paths:
        assert _decode_directory_name(_encode_directory_name(path)) == path


def test_list_pi_sessions(monkeypatch, tmp_path: Path) -> None:
    sessions_dir = tmp_path / ".pi" / "agent" / "sessions"
    monkeypatch.setenv("PI_SESSIONS_DIR", str(sessions_dir))
    monkeypatch.setattr(
        "tether.discovery.pi_sessions.find_running_pi_sessions", lambda: set()
    )

    session_id = "d6660987-06ac-427d-b751-1232e8b88ca2"
    project_dir = sessions_dir / "--home-lars-project--"
    session_file = project_dir / f"2026-02-11T08-00-00-000Z_{session_id}.jsonl"
    _write_pi_session(session_file, session_id)

    sessions = list_pi_sessions()
    assert len(sessions) == 1
    summary = sessions[0]
    assert summary.id == session_id
    assert summary.first_prompt == "Hello pi"
    assert summary.message_count == 4  # 2 user + 2 assistant
    assert summary.directory == "/home/lars/project"
    assert summary.runner_type.value == "pi"
    assert summary.is_running is False


def test_list_pi_sessions_filtered_by_directory(monkeypatch, tmp_path: Path) -> None:
    sessions_dir = tmp_path / ".pi" / "agent" / "sessions"
    monkeypatch.setenv("PI_SESSIONS_DIR", str(sessions_dir))
    monkeypatch.setattr(
        "tether.discovery.pi_sessions.find_running_pi_sessions", lambda: set()
    )

    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    project_dir = sessions_dir / "--home-lars-project--"
    session_file = project_dir / f"2026-02-11T08-00-00-000Z_{session_id}.jsonl"
    _write_pi_session(session_file, session_id)

    # Different directory — should not find it
    sessions = list_pi_sessions(directory="/home/lars/other")
    assert len(sessions) == 0

    # Matching directory — should find it
    sessions = list_pi_sessions(directory="/home/lars/project")
    assert len(sessions) == 1


def test_get_pi_session_detail(monkeypatch, tmp_path: Path) -> None:
    sessions_dir = tmp_path / ".pi" / "agent" / "sessions"
    monkeypatch.setenv("PI_SESSIONS_DIR", str(sessions_dir))
    monkeypatch.setattr(
        "tether.discovery.pi_sessions.find_running_pi_sessions", lambda: set()
    )

    session_id = "d6660987-06ac-427d-b751-1232e8b88ca2"
    project_dir = sessions_dir / "--home-lars-project--"
    session_file = project_dir / f"2026-02-11T08-00-00-000Z_{session_id}.jsonl"
    _write_pi_session(session_file, session_id)

    detail = get_pi_session_detail(session_id)
    assert detail is not None
    assert detail.id == session_id
    assert detail.directory == "/home/lars/project"
    assert detail.runner_type.value == "pi"

    roles = [m.role for m in detail.messages]
    assert roles == ["user", "assistant", "user", "assistant"]

    # First user message
    assert detail.messages[0].content == "Hello pi"

    # Assistant message with thinking
    assert detail.messages[1].content == "Hello! How can I help?"
    assert detail.messages[1].thinking == "User is greeting me"

    # Second user message (plain string content)
    assert detail.messages[2].content == "What files are here?"

    # Assistant with tool call — text extracted, tool call ignored
    assert detail.messages[3].content == "Let me check..."


def test_get_pi_session_detail_not_found(monkeypatch, tmp_path: Path) -> None:
    sessions_dir = tmp_path / ".pi" / "agent" / "sessions"
    monkeypatch.setenv("PI_SESSIONS_DIR", str(sessions_dir))
    sessions_dir.mkdir(parents=True, exist_ok=True)

    detail = get_pi_session_detail("nonexistent-id")
    assert detail is None


def test_list_pi_sessions_empty(monkeypatch, tmp_path: Path) -> None:
    sessions_dir = tmp_path / ".pi" / "agent" / "sessions"
    monkeypatch.setenv("PI_SESSIONS_DIR", str(sessions_dir))
    # Don't create the directory — should return empty
    sessions = list_pi_sessions()
    assert sessions == []
