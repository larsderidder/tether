"""Discover Codex sessions from rollout files and SQLite state."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3

from agent_sessions.models import (
    RunnerType,
    SessionDetail,
    SessionMessage,
    SessionSummary,
)
from agent_sessions.providers.codex import (
    get_codex_session_detail as _get_rollout_session_detail,
    list_codex_sessions as _list_rollout_sessions,
)
from agent_sessions.providers.codex import find_running_codex_sessions
from agent_sessions.path_utils import normalize_directory_path


def list_codex_sessions(
    directory: str | None = None, limit: int = 50
) -> list[SessionSummary]:
    """List Codex sessions from rollout JSONL files and SQLite thread state."""

    sessions_by_id = {
        session.id: session
        for session in _list_rollout_sessions(directory=directory, limit=limit)
    }
    normalized_directory = normalize_directory_path(directory) if directory else None

    for session in _list_sqlite_thread_sessions():
        if (
            normalized_directory
            and normalize_directory_path(session.directory) != normalized_directory
        ):
            continue
        sessions_by_id.setdefault(session.id, session)

    sessions = sorted(
        sessions_by_id.values(), key=lambda item: item.last_activity, reverse=True
    )
    return sessions[:limit]


def get_codex_session_detail(session_id: str, limit: int = 100) -> SessionDetail | None:
    """Get Codex session details from rollout JSONL or SQLite thread state."""

    detail = _get_rollout_session_detail(session_id, limit=limit)
    if detail is not None:
        return detail

    return _get_sqlite_thread_detail(session_id, limit=limit)


def _codex_home() -> Path:
    """Return the configured Codex home directory."""

    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()


def _sqlite_state_files() -> list[Path]:
    """Return candidate Codex SQLite state files."""

    codex_home = _codex_home()
    if not codex_home.exists():
        return []
    return sorted(
        codex_home.glob("*.sqlite"), key=lambda path: path.stat().st_mtime, reverse=True
    )


def _list_sqlite_thread_sessions() -> list[SessionSummary]:
    """Parse Codex thread summaries from SQLite state files."""

    running_sessions = find_running_codex_sessions()
    sessions: list[SessionSummary] = []
    seen: set[str] = set()

    for state_file in _sqlite_state_files():
        try:
            with sqlite3.connect(f"file:{state_file}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT id, cwd, first_user_message, title, updated_at, created_at
                    FROM threads
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        except sqlite3.Error:
            continue

        for row in rows:
            session_id = str(row["id"])
            if session_id in seen:
                continue
            seen.add(session_id)

            first_prompt = str(row["first_user_message"] or row["title"] or "") or None
            sessions.append(
                SessionSummary(
                    id=session_id,
                    runner_type=RunnerType.CODEX,
                    directory=str(row["cwd"]),
                    first_prompt=first_prompt,
                    last_prompt=first_prompt,
                    last_activity=_unix_to_iso(row["updated_at"] or row["created_at"]),
                    message_count=1 if first_prompt else 0,
                    is_running=session_id in running_sessions,
                )
            )

    return sessions


def _get_sqlite_thread_detail(
    session_id: str, limit: int = 100
) -> SessionDetail | None:
    """Build a minimal session detail from Codex SQLite thread state."""

    running_sessions = find_running_codex_sessions()

    for state_file in _sqlite_state_files():
        try:
            with sqlite3.connect(f"file:{state_file}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT id, cwd, first_user_message, title, updated_at, created_at
                    FROM threads
                    WHERE id = ?
                    """,
                    (session_id,),
                ).fetchone()
        except sqlite3.Error:
            continue

        if row is None:
            continue

        first_prompt = str(row["first_user_message"] or row["title"] or "") or None
        messages = []
        if first_prompt and limit != 0:
            messages.append(
                SessionMessage(
                    role="user",
                    content=first_prompt,
                    timestamp=_unix_to_iso(row["created_at"]),
                )
            )

        return SessionDetail(
            id=session_id,
            runner_type=RunnerType.CODEX,
            directory=str(row["cwd"]),
            first_prompt=first_prompt,
            last_prompt=first_prompt,
            last_activity=_unix_to_iso(row["updated_at"] or row["created_at"]),
            message_count=len(messages),
            is_running=session_id in running_sessions,
            messages=messages,
        )

    return None


def _unix_to_iso(value: int | str | None) -> str:
    """Convert Codex SQLite timestamps to UTC ISO strings."""

    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        timestamp = 0
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
