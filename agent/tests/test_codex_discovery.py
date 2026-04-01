"""Tests for Codex session discovery and parsing."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

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


def _write_sqlite_thread(
    path: Path,
    session_id: str,
    *,
    cwd: str = "/home/lars/xithing/tether",
    first_user_message: str = "Hello from sqlite",
    title: str = "Hello from sqlite",
    rollout_path: str = "",
    created_at: int = 1770408000,
    updated_at: int = 1770408060,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                cwd TEXT NOT NULL,
                title TEXT NOT NULL,
                first_user_message TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO threads (id, rollout_path, created_at, updated_at, cwd, title, first_user_message)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, rollout_path, created_at, updated_at, cwd, title, first_user_message),
        )
        conn.commit()


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


def test_list_codex_sessions_from_sqlite_threads(monkeypatch, tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    session_id = "019b2182-8e89-77a1-a675-72857fca4fb1"
    _write_sqlite_thread(
        codex_home / "state_1.sqlite",
        session_id,
        cwd="/tmp/sqlite-only",
        first_user_message="Prompt from sqlite",
        title="Prompt from sqlite",
    )

    sessions = list_codex_sessions()
    assert len(sessions) == 1
    summary = sessions[0]
    assert summary.id == session_id
    assert summary.directory == "/tmp/sqlite-only"
    assert summary.first_prompt == "Prompt from sqlite"
    assert summary.message_count == 1


def test_get_codex_session_detail_from_sqlite_threads(monkeypatch, tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    session_id = "019b2182-8e89-77a1-a675-72857fca4fb1"
    _write_sqlite_thread(
        codex_home / "state_1.sqlite",
        session_id,
        cwd="/tmp/sqlite-detail",
        first_user_message="Prompt from sqlite detail",
        title="Prompt from sqlite detail",
    )

    detail = get_codex_session_detail(session_id)
    assert detail is not None
    assert detail.id == session_id
    assert detail.directory == "/tmp/sqlite-detail"
    assert [m.role for m in detail.messages] == ["user"]
    assert detail.messages[0].content == "Prompt from sqlite detail"
