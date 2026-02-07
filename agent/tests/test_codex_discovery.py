"""Tests for Codex session discovery and parsing."""

from __future__ import annotations

import json
from pathlib import Path

from tether.discovery.codex_sessions import (
    list_codex_sessions,
    get_codex_session_detail,
)


def _write_rollout(path: Path, session_id: str) -> None:
    records = [
        {
            "timestamp": "2026-02-06T20:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": "/home/lars/xithing/tether",
            },
        },
        {
            "timestamp": "2026-02-06T20:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Hello Codex"},
                ],
            },
        },
        {
            "timestamp": "2026-02-06T20:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Hi there"},
                ],
            },
        },
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def test_list_codex_sessions(monkeypatch, tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    session_id = "019b2182-8e89-77a1-a675-72857fca4fb1"
    rollout_path = codex_home / "sessions" / "2026" / "02" / "06" / f"rollout-2026-02-06T20-00-00-{session_id}.jsonl"
    _write_rollout(rollout_path, session_id)

    sessions = list_codex_sessions()
    assert len(sessions) == 1
    summary = sessions[0]
    assert summary.id == session_id
    assert summary.first_prompt == "Hello Codex"
    assert summary.message_count == 2
    assert summary.directory == "/home/lars/xithing/tether"


def test_get_codex_session_detail(monkeypatch, tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    session_id = "019b2182-8e89-77a1-a675-72857fca4fb1"
    rollout_path = codex_home / "sessions" / "2026" / "02" / "06" / f"rollout-2026-02-06T20-00-00-{session_id}.jsonl"
    _write_rollout(rollout_path, session_id)

    detail = get_codex_session_detail(session_id)
    assert detail is not None
    assert detail.id == session_id
    assert detail.directory == "/home/lars/xithing/tether"
    assert [m.role for m in detail.messages] == ["user", "assistant"]
    assert detail.messages[0].content == "Hello Codex"
    assert detail.messages[1].content == "Hi there"
