"""Tests for the incremental pi transcript detail cache."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_sessions.providers.pi as agent_pi
import tether.discovery.pi_detail_cache as pi_cache
from tether.discovery.pi_detail_cache import (
    clear_pi_detail_cache,
    get_pi_session_detail_cached,
)

SESSION_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture(autouse=True)
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the real session-file lookup at a tmp sessions root and clear the cache."""
    root = tmp_path / "sessions"
    project = root / "--home-lars-repo--"
    project.mkdir(parents=True)
    monkeypatch.setattr(agent_pi, "_pi_sessions_dir", lambda: root)
    monkeypatch.setattr(pi_cache, "find_running_pi_sessions", lambda: set())
    clear_pi_detail_cache()
    yield project
    clear_pi_detail_cache()


def _session_file(project: Path, session_id: str = SESSION_ID) -> Path:
    return project / f"2026-01-01T00-00-00-000Z_{session_id}.jsonl"


def _write(path: Path, records: list[dict], *, append: bool = False) -> None:
    mode = "a" if append else "w"
    with open(path, mode, encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _user(text: str, ts: str = "2026-01-01T00:00:01Z") -> dict:
    return {
        "type": "message",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def _assistant(text: str, ts: str = "2026-01-01T00:00:02Z") -> dict:
    return {
        "type": "message",
        "timestamp": ts,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _header() -> dict:
    return {"type": "session", "id": SESSION_ID, "cwd": "/home/lars/repo"}


def test_full_parse_and_unchanged_hit_skips_lookup_and_ps_scan(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached transcript is reused without re-resolving the file or rescanning ps."""
    _write(_session_file(project), [_header(), _user("hello"), _assistant("hi there")])
    real_find = getattr(agent_pi, "_find_session_file")
    find_calls = 0

    def counting_find(session_id: str):
        nonlocal find_calls
        find_calls += 1
        return real_find(session_id)

    monkeypatch.setattr(pi_cache, "_find_session_file", counting_find)
    ps_calls = 0

    def counting_ps() -> set[str]:
        nonlocal ps_calls
        ps_calls += 1
        return {SESSION_ID}

    monkeypatch.setattr(pi_cache, "find_running_pi_sessions", counting_ps)

    first = get_pi_session_detail_cached(SESSION_ID)
    second = get_pi_session_detail_cached(SESSION_ID)

    assert first is not None and second is not None
    assert [m.role for m in first.messages] == ["user", "assistant"]
    assert first.directory == "/home/lars/repo"
    assert first.first_prompt == "hello"
    assert first.last_prompt == "hello"
    assert first.message_count == 2
    assert first.is_running is True
    assert first.messages == second.messages
    assert find_calls == 1
    assert ps_calls == 1


def test_incremental_append_parses_only_new_records(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Appended lines extend the cached parse instead of reparsing the file."""
    path = _session_file(project)
    _write(path, [_header(), _user("first")])
    parsed: list[dict] = []
    real_apply = getattr(pi_cache._PiDetailCache, "_apply_record")

    def counting_apply(state, record):
        parsed.append(record)
        return real_apply(state, record)

    monkeypatch.setattr(
        pi_cache._PiDetailCache, "_apply_record", staticmethod(counting_apply)
    )

    detail = get_pi_session_detail_cached(SESSION_ID)
    assert detail is not None and detail.message_count == 1
    assert len(parsed) == 2  # session header + user message

    _write(path, [_assistant("second")], append=True)
    detail = get_pi_session_detail_cached(SESSION_ID)
    assert detail is not None
    assert [m.content for m in detail.messages] == ["first", "second"]
    assert len(parsed) == 3  # only the appended assistant record was parsed

    detail = get_pi_session_detail_cached(SESSION_ID)
    assert detail is not None and detail.message_count == 2
    assert len(parsed) == 3  # unchanged file parses nothing


def test_partial_line_is_buffered_until_newline(project: Path) -> None:
    """A trailing line without a newline is not parsed until completed."""
    path = _session_file(project)
    _write(path, [_header(), _user("first")])
    assert get_pi_session_detail_cached(SESSION_ID).message_count == 1

    record = json.dumps(_assistant("partial"))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(record)  # no trailing newline yet
    detail = get_pi_session_detail_cached(SESSION_ID)
    assert detail is not None and detail.message_count == 1

    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n")
    detail = get_pi_session_detail_cached(SESSION_ID)
    assert detail is not None
    assert [m.content for m in detail.messages] == ["first", "partial"]


def test_truncated_or_replaced_file_reparses_from_start(project: Path) -> None:
    """A shrunk transcript resets the cursor and drops stale messages."""
    path = _session_file(project)
    _write(path, [_header(), _user("old one"), _assistant("old two")])
    detail = get_pi_session_detail_cached(SESSION_ID)
    assert detail is not None and detail.message_count == 2

    _write(path, [_header(), _user("fresh")])
    detail = get_pi_session_detail_cached(SESSION_ID)
    assert detail is not None
    assert [m.content for m in detail.messages] == ["fresh"]
    assert detail.first_prompt == "fresh"


def test_limit_keeps_only_the_tail(project: Path) -> None:
    """The message limit truncates to the most recent messages."""
    _write(
        _session_file(project),
        [_header(), _user("one"), _assistant("two"), _user("three")],
    )
    detail = get_pi_session_detail_cached(SESSION_ID, limit=2)
    assert detail is not None
    assert [m.content for m in detail.messages] == ["two", "three"]
    assert detail.message_count == 2


def test_assistant_thinking_is_extracted(project: Path) -> None:
    """Thinking blocks land in the thinking field like the upstream parser."""
    record = {
        "type": "message",
        "timestamp": "2026-01-01T00:00:02Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "pondering"},
                {"type": "text", "text": "answer"},
            ],
        },
    }
    _write(_session_file(project), [_header(), _user("q"), record])
    detail = get_pi_session_detail_cached(SESSION_ID)
    assert detail is not None
    assistant = detail.messages[1]
    assert assistant.content == "answer"
    assert assistant.thinking == "pondering"


def test_unknown_session_returns_none(project: Path) -> None:
    """A session id without a transcript file yields None."""
    assert get_pi_session_detail_cached(OTHER_ID) is None
