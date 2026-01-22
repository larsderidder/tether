"""Session storage and runtime process state."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections import deque
import re
from datetime import datetime, timedelta, timezone
from threading import Lock

from tether.git import has_git_repository, normalize_directory_path
from tether.models import Message, RepoRef, Session, SessionState


class SessionStore:
    """Session registry with SQLite persistence and per-session process bookkeeping."""

    def __init__(self) -> None:
        self._data_dir = os.environ.get("AGENT_DATA_DIR") or os.path.join(
            os.path.dirname(__file__), "..", "data"
        )
        self._data_dir = os.path.abspath(self._data_dir)
        self._db_path = os.path.join(self._data_dir, "sessions.db")
        self._db_lock = Lock()
        os.makedirs(self._data_dir, exist_ok=True)
        os.makedirs(os.path.join(self._data_dir, "sessions"), exist_ok=True)
        self._db = sqlite3.connect(self._db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL;")
        self._db.execute("PRAGMA synchronous=NORMAL;")
        self._init_db()
        self._sessions: dict[str, Session] = {}
        self._seq: dict[str, int] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._workdirs: dict[str, str] = {}
        self._master_fds: dict[str, int] = {}
        self._stdins: dict[str, asyncio.StreamWriter] = {}
        self._input_locks: dict[str, asyncio.Lock] = {}
        self._prompt_sent: dict[str, bool] = {}
        self._pending_inputs: dict[str, list[str]] = {}
        self._runner_session_ids: dict[str, str] = {}
        self._recent_output: dict[str, deque[str]] = {}
        self._workdir_managed: dict[str, bool] = {}
        self._claude_tasks: dict[str, asyncio.Task] = {}
        self._stop_requested: dict[str, bool] = {}
        self._load_sessions()

    def _init_db(self) -> None:
        with self._db_lock:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    repo_id TEXT NOT NULL,
                    repo_display TEXT NOT NULL,
                    repo_ref_type TEXT NOT NULL,
                    repo_ref_value TEXT NOT NULL,
                    state TEXT NOT NULL,
                    name TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    last_activity_at TEXT NOT NULL,
                    exit_code INTEGER,
                    summary TEXT,
                    runner_header TEXT,
                    directory TEXT,
                    directory_has_git INTEGER DEFAULT 0,
                    workdir_managed INTEGER DEFAULT 0
                )
                """)
            self._ensure_header_column()
            self._ensure_column("sessions", "directory", "TEXT")
            self._ensure_column("sessions", "directory_has_git", "INTEGER DEFAULT 0")
            self._ensure_column("sessions", "workdir_managed", "INTEGER DEFAULT 0")
            self._ensure_column("sessions", "runner_type", "TEXT")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    created_at TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                )
                """)
            self._db.commit()

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        existing = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
        if column in existing:
            return
        self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _ensure_header_column(self) -> None:
        existing = {row[1] for row in self._db.execute("PRAGMA table_info(sessions)")}
        if "runner_header" in existing:
            return
        if "codex_header" in existing:
            try:
                self._db.execute(
                    "ALTER TABLE sessions RENAME COLUMN codex_header TO runner_header"
                )
                return
            except sqlite3.OperationalError:
                self._db.execute("ALTER TABLE sessions ADD COLUMN runner_header TEXT")
                self._db.execute(
                    "UPDATE sessions SET runner_header = codex_header WHERE runner_header IS NULL"
                )
                return
        self._db.execute("ALTER TABLE sessions ADD COLUMN runner_header TEXT")

    def _load_sessions(self) -> None:
        with self._db_lock:
            rows = self._db.execute("SELECT * FROM sessions").fetchall()
        for row in rows:
            session, workdir_managed = self._session_from_row(row)
            self._sessions[session.id] = session
            self._seq[session.id] = 0
            self._subscribers.setdefault(session.id, [])
            if session.directory:
                self._workdirs[session.id] = session.directory
                self._workdir_managed[session.id] = workdir_managed

    def _session_from_row(self, row: sqlite3.Row) -> tuple[Session, bool]:
        keys = set(row.keys())
        runner_header = None
        if "runner_header" in keys:
            runner_header = row["runner_header"]
        elif "codex_header" in keys:
            runner_header = row["codex_header"]
        runner_type = row["runner_type"] if "runner_type" in keys else None
        return (
            Session(
                id=row["id"],
                repo_id=row["repo_id"],
                repo_display=row["repo_display"],
                repo_ref=RepoRef(
                    type=row["repo_ref_type"], value=row["repo_ref_value"]
                ),
                state=SessionState(row["state"]),
                name=row["name"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                last_activity_at=row["last_activity_at"],
                exit_code=row["exit_code"],
                summary=row["summary"],
                runner_header=runner_header,
                runner_type=runner_type,
                directory=row["directory"],
                directory_has_git=bool(row["directory_has_git"]),
            ),
            bool(row["workdir_managed"]),
        )

    def _now(self) -> str:
        """Return an ISO8601 UTC timestamp suitable for API payloads."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _parse_ts(self, value: str) -> datetime:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )

    def create_session(self, repo_id: str, base_ref: str | None) -> Session:
        """Create and register a new session in CREATED state.

        Args:
            repo_id: Identifier for the repo being worked on.
            base_ref: Optional base ref name or branch.
        """
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        now = self._now()
        session = Session(
            id=session_id,
            repo_id=repo_id,
            repo_display=repo_id,
            repo_ref=RepoRef(type="path", value=repo_id),
            state=SessionState.CREATED,
            name="New session",
            created_at=now,
            started_at=None,
            ended_at=None,
            last_activity_at=now,
            exit_code=None,
            summary=None,
            runner_header=None,
        )
        self._sessions[session_id] = session
        self._seq[session_id] = 0
        self._subscribers.setdefault(session_id, [])
        self._persist_session(session)
        return session

    def list_sessions(self) -> list[Session]:
        """Return all sessions currently tracked in memory."""
        return list(self._sessions.values())

    def get_session(self, session_id: str) -> Session | None:
        """Fetch a session by id, or None if missing."""
        return self._sessions.get(session_id)

    def update_session(self, session: Session) -> None:
        """Persist an updated session snapshot."""
        self._sessions[session.id] = session
        self._persist_session(session)

    def delete_session(self, session_id: str) -> bool:
        """Remove a session and its associated runtime state."""
        session = self._sessions.pop(session_id, None)
        if not session:
            return False
        with self._db_lock:
            self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self._db.commit()
        self._seq.pop(session_id, None)
        self._subscribers.pop(session_id, None)
        self.clear_process(session_id)
        self.clear_master_fd(session_id)
        self.clear_stdin(session_id)
        self.clear_prompt_sent(session_id)
        self.clear_pending_inputs(session_id)
        self.clear_runner_session_id(session_id)
        self.clear_last_output(session_id)
        self.clear_workdir(session_id)
        self.clear_claude_task(session_id)
        self.clear_stop_requested(session_id)
        return True

    def clear_all_data(self) -> None:
        """Delete all persisted sessions and in-memory session state."""
        with self._db_lock:
            self._db.execute("DELETE FROM messages")
            self._db.execute("DELETE FROM sessions")
            self._db.commit()
        self._sessions.clear()
        self._seq.clear()
        self._subscribers.clear()
        self._procs.clear()
        self._workdirs.clear()
        self._master_fds.clear()
        self._stdins.clear()
        self._input_locks.clear()
        self._prompt_sent.clear()
        self._pending_inputs.clear()
        self._runner_session_ids.clear()
        self._recent_output.clear()
        self._workdir_managed.clear()
        self._claude_tasks.clear()
        self._stop_requested.clear()
        logs_root = os.path.join(self._data_dir, "sessions")
        shutil.rmtree(logs_root, ignore_errors=True)
        os.makedirs(logs_root, exist_ok=True)

    def next_seq(self, session_id: str) -> int:
        """Advance and return the per-session event sequence counter."""
        current = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = current
        return current

    def new_subscriber(self, session_id: str) -> asyncio.Queue:
        """Register a new SSE subscriber queue for a session.

        Args:
            session_id: Internal session identifier.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(session_id, []).append(queue)
        return queue

    def remove_subscriber(self, session_id: str, queue: asyncio.Queue) -> None:
        """Unregister an SSE subscriber queue."""
        queues = self._subscribers.get(session_id, [])
        if queue in queues:
            queues.remove(queue)

    async def emit(self, session_id: str, event: dict) -> None:
        """Broadcast an event payload to all session subscribers.

        Args:
            session_id: Internal session identifier.
            event: Event payload to broadcast.
        """
        for queue in list(self._subscribers.get(session_id, [])):
            await queue.put(event)
        self._append_event_log(session_id, event)

    def _persist_session(self, session: Session) -> None:
        with self._db_lock:
            self._db.execute(
                """
                INSERT INTO sessions (
                    id, repo_id, repo_display, repo_ref_type, repo_ref_value, state,
                    name, created_at, started_at, ended_at, last_activity_at,
                    exit_code, summary, runner_header, runner_type,
                    directory, directory_has_git, workdir_managed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    repo_id=excluded.repo_id,
                    repo_display=excluded.repo_display,
                    repo_ref_type=excluded.repo_ref_type,
                    repo_ref_value=excluded.repo_ref_value,
                    state=excluded.state,
                    name=excluded.name,
                    created_at=excluded.created_at,
                    started_at=excluded.started_at,
                    ended_at=excluded.ended_at,
                    last_activity_at=excluded.last_activity_at,
                    exit_code=excluded.exit_code,
                    summary=excluded.summary,
                    runner_header=excluded.runner_header,
                    runner_type=excluded.runner_type,
                    directory=excluded.directory,
                    directory_has_git=excluded.directory_has_git,
                    workdir_managed=excluded.workdir_managed
                """,
                (
                    session.id,
                    session.repo_id,
                    session.repo_display,
                    session.repo_ref.type,
                    session.repo_ref.value,
                    session.state.value,
                    session.name,
                    session.created_at,
                    session.started_at,
                    session.ended_at,
                    session.last_activity_at,
                    session.exit_code,
                    session.summary,
                    session.runner_header,
                    session.runner_type,
                    session.directory,
                    int(session.directory_has_git),
                    int(self._workdir_managed.get(session.id, False)),
                ),
            )
            self._db.commit()

    def _append_event_log(self, session_id: str, event: dict) -> None:
        path = os.path.join(self._data_dir, "sessions", session_id, "events.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        max_bytes = int(os.environ.get("AGENT_EVENT_LOG_MAX_BYTES", "5000000"))
        if max_bytes > 0 and os.path.exists(path):
            try:
                if os.path.getsize(path) > max_bytes:
                    rotated = f"{path}.1"
                    if os.path.exists(rotated):
                        os.remove(rotated)
                    os.replace(path, rotated)
            except OSError:
                pass
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")

    def prune_sessions(self, retention_days: int) -> int:
        """Delete sessions (and logs) older than the retention window."""
        if retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        removed = 0
        for session in list(self._sessions.values()):
            if session.state in (SessionState.RUNNING, SessionState.STOPPING):
                continue
            ts = session.ended_at or session.last_activity_at or session.created_at
            if not ts:
                continue
            try:
                when = self._parse_ts(ts)
            except ValueError:
                continue
            if when < cutoff:
                if self.delete_session(session.id):
                    removed += 1
        return removed

    def set_process(self, session_id: str, proc: asyncio.subprocess.Process) -> None:
        """Track the subprocess running for a session."""
        self._procs[session_id] = proc

    def get_process(self, session_id: str) -> asyncio.subprocess.Process | None:
        """Return the tracked subprocess, if any."""
        return self._procs.get(session_id)

    def clear_process(self, session_id: str) -> None:
        self._procs.pop(session_id, None)

    def set_master_fd(self, session_id: str, fd: int) -> None:
        """Track the PTY master fd for a session."""
        self._master_fds[session_id] = fd

    def get_master_fd(self, session_id: str) -> int | None:
        """Return the PTY master fd, if present."""
        return self._master_fds.get(session_id)

    def clear_master_fd(self, session_id: str) -> None:
        self._master_fds.pop(session_id, None)

    def set_stdin(self, session_id: str, stdin: asyncio.StreamWriter) -> None:
        """Store the stdin stream for a session's subprocess."""
        self._stdins[session_id] = stdin

    def get_stdin(self, session_id: str) -> asyncio.StreamWriter | None:
        """Return the stored stdin stream for a session."""
        return self._stdins.get(session_id)

    def clear_stdin(self, session_id: str) -> None:
        self._stdins.pop(session_id, None)

    def get_input_lock(self, session_id: str) -> asyncio.Lock:
        """Return a per-session lock guarding stdin writes."""
        lock = self._input_locks.get(session_id)
        if not lock:
            lock = asyncio.Lock()
            self._input_locks[session_id] = lock
        return lock

    def is_prompt_sent(self, session_id: str) -> bool:
        """Check whether the initial prompt was sent to the runner."""
        return self._prompt_sent.get(session_id, False)

    def mark_prompt_sent(self, session_id: str) -> None:
        """Record that the initial prompt was sent to the runner."""
        self._prompt_sent[session_id] = True

    def clear_prompt_sent(self, session_id: str) -> None:
        self._prompt_sent.pop(session_id, None)

    def add_pending_input(self, session_id: str, text: str) -> None:
        """Queue input to send once the runner is ready."""
        self._pending_inputs.setdefault(session_id, []).append(text)

    def pop_pending_inputs(self, session_id: str) -> list[str]:
        """Drain and return all pending inputs."""
        return self._pending_inputs.pop(session_id, [])

    def clear_pending_inputs(self, session_id: str) -> None:
        self._pending_inputs.pop(session_id, None)

    def pop_next_pending_input(self, session_id: str) -> str | None:
        """Pop the next queued input, if any."""
        queue = self._pending_inputs.get(session_id)
        if not queue:
            return None
        item = queue.pop(0)
        if not queue:
            self._pending_inputs.pop(session_id, None)
        return item

    def has_pending_inputs(self, session_id: str) -> bool:
        """Return True if there is queued input."""
        return bool(self._pending_inputs.get(session_id))

    def set_runner_session_id(self, session_id: str, runner_session_id: str) -> None:
        """Store the runner-specific session id."""
        self._runner_session_ids[session_id] = runner_session_id

    def get_runner_session_id(self, session_id: str) -> str | None:
        """Fetch the runner-specific session id."""
        return self._runner_session_ids.get(session_id)

    def clear_runner_session_id(self, session_id: str) -> None:
        self._runner_session_ids.pop(session_id, None)

    def should_emit_output(self, session_id: str, text: str) -> bool:
        """Return True if output is non-empty and not recently emitted.

        Args:
            session_id: Internal session identifier.
            text: Raw output text.
        """
        normalized = self._normalize_output(text)
        if not normalized:
            return False
        history = self._recent_output.get(session_id)
        if history is None:
            history = deque(maxlen=10)
            self._recent_output[session_id] = history
        if normalized in history:
            return False
        history.append(normalized)
        return True

    def _normalize_output(self, text: str) -> str:
        """Normalize output to de-duplicate noisy repeated lines.

        Args:
            text: Output text to normalize.
        """
        # Strip ANSI codes and collapse whitespace for stable comparisons.
        stripped = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
        compact = " ".join(stripped.strip().split())
        return compact

    def clear_last_output(self, session_id: str) -> None:
        self._recent_output.pop(session_id, None)

    def get_recent_output(self, session_id: str) -> list[str]:
        """Get recent output chunks for a session.

        Args:
            session_id: Internal session identifier.

        Returns:
            List of recent output strings (up to 10).
        """
        return list(self._recent_output.get(session_id, []))

    def set_workdir(self, session_id: str, path: str, *, managed: bool) -> str:
        """Record a working directory and update the session metadata."""
        normalized = normalize_directory_path(path)
        self._workdirs[session_id] = normalized
        self._workdir_managed[session_id] = managed
        session = self._sessions.get(session_id)
        if session:
            session.directory = normalized
            session.directory_has_git = has_git_repository(normalized)
            self.update_session(session)
        return normalized

    def create_workdir(self, session_id: str) -> str:
        """Create a temporary working directory for the session."""
        path = tempfile.mkdtemp(prefix=f"tether_{session_id}_")
        return self.set_workdir(session_id, path, managed=True)

    def get_workdir(self, session_id: str) -> str | None:
        """Return the session working directory, if created."""
        return self._workdirs.get(session_id)

    def clear_workdir(self, session_id: str, *, force: bool = True) -> None:
        managed = self._workdir_managed.get(session_id, False)
        if not force and not managed:
            return
        path = self._workdirs.pop(session_id, None)
        self._workdir_managed.pop(session_id, None)
        if path and managed:
            shutil.rmtree(path, ignore_errors=True)

    def add_message(self, session_id: str, role: str, content: object) -> Message:
        """Add a message to conversation history.

        Args:
            session_id: Internal session identifier.
            role: Message role ("user" or "assistant").
            content: Content blocks (will be JSON-encoded).
        """
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        now = self._now()
        with self._db_lock:
            seq_row = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = seq_row[0] if seq_row else 1
            content_json = json.dumps(content)
            self._db.execute(
                """
                INSERT INTO messages (id, session_id, role, content, created_at, seq)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (message_id, session_id, role, content_json, now, seq),
            )
            self._db.commit()
        return Message(
            id=message_id,
            session_id=session_id,
            role=role,
            content=content_json,
            seq=seq,
            created_at=now,
        )

    def get_messages(self, session_id: str) -> list[dict]:
        """Get conversation history for a session.

        Args:
            session_id: Internal session identifier.

        Returns:
            List of message dicts with role and content for Anthropic API.
        """
        with self._db_lock:
            rows = self._db.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        messages = []
        for row in rows:
            content = json.loads(row[1]) if row[1] else []
            messages.append({"role": row[0], "content": content})
        return messages

    def clear_messages(self, session_id: str) -> None:
        """Clear conversation history for a session.

        Args:
            session_id: Internal session identifier.
        """
        with self._db_lock:
            self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self._db.commit()

    def set_claude_task(self, session_id: str, task: asyncio.Task) -> None:
        """Track the Claude conversation loop task for a session."""
        self._claude_tasks[session_id] = task

    def get_claude_task(self, session_id: str) -> asyncio.Task | None:
        """Return the Claude conversation loop task, if any."""
        return self._claude_tasks.get(session_id)

    def clear_claude_task(self, session_id: str) -> None:
        self._claude_tasks.pop(session_id, None)

    def request_stop(self, session_id: str) -> None:
        """Signal the Claude conversation loop to stop."""
        self._stop_requested[session_id] = True

    def is_stop_requested(self, session_id: str) -> bool:
        """Check if stop was requested for a session."""
        return self._stop_requested.get(session_id, False)

    def clear_stop_requested(self, session_id: str) -> None:
        self._stop_requested.pop(session_id, None)


store = SessionStore()
