"""Session storage and runtime process state."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from collections import deque
from dataclasses import dataclass, field
import re
from datetime import datetime, timedelta, timezone
from threading import Lock

import structlog
from sqlalchemy import func as sa_func
from sqlmodel import select

from tether.db import get_session as get_db_session, init_db
from tether.git import has_git_repository, normalize_directory_path
from tether.models import Message, RepoRef, Session, SessionState
from tether.settings import settings

logger = structlog.get_logger(__name__)


@dataclass
class PendingPermission:
    """A permission request waiting for user response."""

    request_id: str
    tool_name: str
    tool_input: dict
    future: asyncio.Future
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SessionRuntime:
    """Per-session runtime state (not persisted to database)."""

    seq: int = 0
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    proc: asyncio.subprocess.Process | None = None
    pending_inputs: list[str] = field(default_factory=list)
    recent_output: deque[str] = field(default_factory=lambda: deque(maxlen=10))
    output_buffer: list[str] = field(default_factory=list)
    stop_requested: bool = False
    synced_message_count: int = 0
    synced_turn_count: int = 0  # Number of conversation turns (user messages)
    pending_permissions: dict[str, PendingPermission] = field(default_factory=dict)
    agent_metadata: dict | None = None  # External agent metadata (not persisted)


class SessionStore:
    """Session registry with SQLModel persistence and per-session process bookkeeping."""

    def __init__(self) -> None:
        self._data_dir = settings.data_dir()
        self._db_lock = Lock()
        os.makedirs(self._data_dir, exist_ok=True)
        os.makedirs(os.path.join(self._data_dir, "sessions"), exist_ok=True)
        self._sessions: dict[str, Session] = {}
        self._runtime: dict[str, SessionRuntime] = {}
        self._load_sessions()

    def _get_runtime(self, session_id: str) -> SessionRuntime:
        """Get or create runtime state for a session."""
        if session_id not in self._runtime:
            # Initialize seq from event log to avoid conflicts after restart
            max_seq = self._get_max_seq_from_log(session_id)
            self._runtime[session_id] = SessionRuntime(seq=max_seq)
        return self._runtime[session_id]

    def _load_sessions(self) -> None:
        init_db()  # Ensure tables exist before querying
        with self._db_lock:
            with get_db_session() as db:
                rows = db.exec(select(Session)).all()
                for row in rows:
                    self._sessions[row.id] = row
                    # Initialize seq from event log to avoid conflicts after restart
                    max_seq = self._get_max_seq_from_log(row.id)
                    self._runtime[row.id] = SessionRuntime(seq=max_seq)

    def _get_max_seq_from_log(self, session_id: str) -> int:
        """Read the max seq from the event log file.

        Used to initialize the seq counter after restart to avoid conflicts
        where new events get seq numbers that collide with old events.
        """
        path = os.path.join(self._data_dir, "sessions", session_id, "events.jsonl")
        if not os.path.exists(path):
            return 0
        max_seq = 0
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                        seq = int(event.get("seq") or 0)
                        if seq > max_seq:
                            max_seq = seq
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            pass
        return max_seq

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
            repo_ref_type="path",
            repo_ref_value=repo_id,
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
        self._runtime[session_id] = SessionRuntime()
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
        existing = self._sessions.get(session.id)
        if existing is not None:
            # Hard invariant: runner_session_id is immutable once set. This prevents
            # accidental "takeover" by overwriting the binding via a full-session
            # update/merge.
            if existing.runner_session_id is not None:
                if session.runner_session_id != existing.runner_session_id:
                    logger.warning(
                        "Refusing to modify runner_session_id via update_session",
                        session_id=session.id,
                        existing=existing.runner_session_id,
                        attempted=session.runner_session_id,
                    )
                    session.runner_session_id = existing.runner_session_id
            else:
                # If not set yet, only allow setting it via set_runner_session_id()
                # or runner_events header capture; update_session shouldn't be used
                # as a generic setter for this field.
                if session.runner_session_id is not None:
                    logger.warning(
                        "Ignoring runner_session_id set via update_session; use set_runner_session_id",
                        session_id=session.id,
                        attempted=session.runner_session_id,
                    )
                    session.runner_session_id = None

        self._sessions[session.id] = session
        self._persist_session(session)

    def delete_session(self, session_id: str) -> bool:
        """Remove a session and its associated runtime state."""
        session = self._sessions.pop(session_id, None)
        if not session:
            return False
        with self._db_lock:
            with get_db_session() as db:
                # Delete messages first
                messages = db.exec(select(Message).where(Message.session_id == session_id)).all()
                for msg in messages:
                    db.delete(msg)
                # Delete session
                db_session = db.get(Session, session_id)
                if db_session:
                    db.delete(db_session)
                db.commit()
        self.clear_workdir(session_id)
        self._runtime.pop(session_id, None)
        return True

    def clear_all_data(self) -> None:
        """Delete all persisted sessions and in-memory session state."""
        with self._db_lock:
            with get_db_session() as db:
                # Delete all messages
                messages = db.exec(select(Message)).all()
                for msg in messages:
                    db.delete(msg)
                # Delete all sessions
                sessions = db.exec(select(Session)).all()
                for sess in sessions:
                    db.delete(sess)
                db.commit()
        self._sessions.clear()
        self._runtime.clear()
        logs_root = os.path.join(self._data_dir, "sessions")
        shutil.rmtree(logs_root, ignore_errors=True)
        os.makedirs(logs_root, exist_ok=True)

    def next_seq(self, session_id: str) -> int:
        """Advance and return the per-session event sequence counter."""
        runtime = self._get_runtime(session_id)
        runtime.seq += 1
        return runtime.seq

    def new_subscriber(self, session_id: str) -> asyncio.Queue:
        """Register a new SSE subscriber queue for a session.

        Args:
            session_id: Internal session identifier.
        """
        queue: asyncio.Queue = asyncio.Queue()
        runtime = self._get_runtime(session_id)
        runtime.subscribers.append(queue)
        logger.debug(
            "New SSE subscriber",
            session_id=session_id,
            total_subscribers=len(runtime.subscribers),
        )
        return queue

    def remove_subscriber(self, session_id: str, queue: asyncio.Queue) -> None:
        """Unregister an SSE subscriber queue."""
        runtime = self._runtime.get(session_id)
        if runtime and queue in runtime.subscribers:
            runtime.subscribers.remove(queue)

    async def emit(self, session_id: str, event: dict) -> None:
        """Broadcast an event payload to all session subscribers.

        Args:
            session_id: Internal session identifier.
            event: Event payload to broadcast.
        """
        runtime = self._runtime.get(session_id)
        subscribers = runtime.subscribers if runtime else []
        event_type = event.get("type")
        if event_type != "heartbeat":
            logger.debug(
                "Broadcasting event",
                session_id=session_id,
                event_type=event_type,
                subscriber_count=len(subscribers),
            )
        for queue in list(subscribers):
            await queue.put(event)
        self._append_event_log(session_id, event)

    def _persist_session(self, session: Session, *, allow_runner_session_id_change: bool = False) -> None:
        with self._db_lock:
            with get_db_session() as db:
                # Low-level safety invariant: runner_session_id is immutable once
                # persisted. This prevents "takeover" via any full-object merge
                # that happens to carry a cleared/changed value.
                if not allow_runner_session_id_change:
                    try:
                        existing = db.get(Session, session.id)
                        if existing and existing.runner_session_id is not None:
                            if session.runner_session_id != existing.runner_session_id:
                                logger.warning(
                                    "Refusing to modify persisted runner_session_id",
                                    session_id=session.id,
                                    existing=existing.runner_session_id,
                                    attempted=session.runner_session_id,
                                )
                                session.runner_session_id = existing.runner_session_id
                    except Exception:
                        # Never fail persistence because the guard couldn't run.
                        logger.exception(
                            "Failed runner_session_id immutability check; proceeding",
                            session_id=session.id,
                        )
                db.merge(session)
                db.commit()

    def _append_event_log(self, session_id: str, event: dict) -> None:
        path = os.path.join(self._data_dir, "sessions", session_id, "events.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        max_bytes = 5_000_000  # 5MB
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
            if session.state in (SessionState.RUNNING, SessionState.INTERRUPTING):
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
        self._get_runtime(session_id).proc = proc

    def get_process(self, session_id: str) -> asyncio.subprocess.Process | None:
        """Return the tracked subprocess, if any."""
        runtime = self._runtime.get(session_id)
        return runtime.proc if runtime else None

    def clear_process(self, session_id: str) -> None:
        runtime = self._runtime.get(session_id)
        if runtime:
            runtime.proc = None

    def add_pending_input(self, session_id: str, text: str) -> None:
        """Queue input to send once the runner is ready."""
        self._get_runtime(session_id).pending_inputs.append(text)

    def pop_pending_inputs(self, session_id: str) -> list[str]:
        """Drain and return all pending inputs."""
        runtime = self._runtime.get(session_id)
        if not runtime:
            return []
        inputs = runtime.pending_inputs[:]
        runtime.pending_inputs.clear()
        return inputs

    def clear_pending_inputs(self, session_id: str) -> None:
        runtime = self._runtime.get(session_id)
        if runtime:
            runtime.pending_inputs.clear()

    def pop_next_pending_input(self, session_id: str) -> str | None:
        """Pop the next queued input, if any."""
        runtime = self._runtime.get(session_id)
        if not runtime or not runtime.pending_inputs:
            return None
        return runtime.pending_inputs.pop(0)

    def has_pending_inputs(self, session_id: str) -> bool:
        """Return True if there is queued input."""
        runtime = self._runtime.get(session_id)
        return bool(runtime and runtime.pending_inputs)

    def set_runner_session_id(self, session_id: str, runner_session_id: str) -> None:
        """Store the runner-specific session id and persist to database.

        IMPORTANT: A session's runner_session_id should never change once set.
        This maintains a stable 1:1 mapping between Tether sessions and external
        Claude sessions. If the session already has a runner_session_id, this
        call is ignored (with a warning if the IDs differ).
        """
        session = self._sessions.get(session_id)
        if not session:
            return

        # Never change an existing runner_session_id
        if session.runner_session_id is not None:
            if session.runner_session_id != runner_session_id:
                logger.warning(
                    "Ignoring attempt to change runner_session_id",
                    session_id=session_id,
                    existing=session.runner_session_id,
                    attempted=runner_session_id,
                )
            return

        # Check if another session already has this runner_session_id
        existing_session_id = self.find_session_by_runner_session_id(runner_session_id)
        if existing_session_id and existing_session_id != session_id:
            logger.warning(
                "runner_session_id already belongs to another session",
                this_session_id=session_id,
                other_session_id=existing_session_id,
                runner_session_id=runner_session_id,
            )
            # Don't steal from another session - just skip setting it
            return

        session.runner_session_id = runner_session_id
        self._persist_session(session)

    def get_runner_session_id(self, session_id: str) -> str | None:
        """Fetch the runner-specific session id."""
        session = self._sessions.get(session_id)
        return session.runner_session_id if session else None

    def clear_runner_session_id(self, session_id: str, *, force: bool = False) -> None:
        """Clear the runner-specific session id.

        This is intentionally guarded: clearing a binding makes it possible to
        accidentally attach a Tether session to a different external session on
        the next run. Only use with `force=True` in explicit maintenance flows.
        """
        if not force:
            logger.warning(
                "Refusing to clear runner_session_id without force",
                session_id=session_id,
            )
            return
        session = self._sessions.get(session_id)
        if session:
            session.runner_session_id = None
            self._persist_session(session, allow_runner_session_id_change=True)

    def replace_runner_session_id(
        self, session_id: str, old_id: str, new_id: str
    ) -> None:
        """Replace runner_session_id when the SDK created a new session.

        This is specifically for the case where we asked the SDK to resume
        session ``old_id`` but it created ``new_id`` instead (e.g. old session
        expired).  The store binding must be updated so future turns resume
        the correct session.
        """
        session = self._sessions.get(session_id)
        if not session:
            return

        if session.runner_session_id != old_id:
            logger.warning(
                "replace_runner_session_id: current binding does not match old_id",
                session_id=session_id,
                current=session.runner_session_id,
                old_id=old_id,
                new_id=new_id,
            )
            # If current binding is None, allow setting the new one directly
            if session.runner_session_id is not None:
                return

        # Check the new ID isn't already taken by another session
        existing_session_id = self.find_session_by_runner_session_id(new_id)
        if existing_session_id and existing_session_id != session_id:
            logger.warning(
                "replace_runner_session_id: new_id already belongs to another session",
                this_session_id=session_id,
                other_session_id=existing_session_id,
                new_id=new_id,
            )
            return

        logger.info(
            "Replacing expired runner_session_id",
            session_id=session_id,
            old_id=old_id,
            new_id=new_id,
        )
        session.runner_session_id = new_id
        self._persist_session(session, allow_runner_session_id_change=True)

    def find_session_by_runner_session_id(self, runner_session_id: str) -> str | None:
        """Find a Tether session ID that is attached to the given runner session ID.

        Args:
            runner_session_id: The external/runner session ID to look up.

        Returns:
            The Tether session ID if found, None otherwise.
        """
        for session in self._sessions.values():
            if session.runner_session_id == runner_session_id:
                return session.id
        return None

    def set_synced_message_count(
        self, session_id: str, count: int, turn_count: int | None = None
    ) -> None:
        """Store the number of messages synced from external session.

        Args:
            session_id: Internal session identifier.
            count: Total number of messages synced.
            turn_count: Number of conversation turns (user messages). If None,
                        uses count for backwards compatibility.
        """
        runtime = self._get_runtime(session_id)
        runtime.synced_message_count = count
        runtime.synced_turn_count = turn_count if turn_count is not None else count

    def get_synced_message_count(self, session_id: str) -> int:
        """Get the number of messages previously synced from external session."""
        runtime = self._runtime.get(session_id)
        return runtime.synced_message_count if runtime else 0

    def get_synced_turn_count(self, session_id: str) -> int:
        """Get the number of conversation turns synced from external session."""
        runtime = self._runtime.get(session_id)
        return runtime.synced_turn_count if runtime else 0

    def should_emit_output(self, session_id: str, text: str) -> bool:
        """Return True if output is non-empty and not recently emitted.

        Args:
            session_id: Internal session identifier.
            text: Raw output text.
        """
        normalized = self._normalize_output(text)
        if not normalized:
            return False
        runtime = self._get_runtime(session_id)
        if normalized in runtime.recent_output:
            return False
        runtime.recent_output.append(normalized)
        return True

    def append_output(self, session_id: str, text: str) -> None:
        """Append raw output text for final aggregation."""
        runtime = self._get_runtime(session_id)
        runtime.output_buffer.append(text)

    def consume_output(self, session_id: str) -> str:
        """Return and clear the aggregated output buffer."""
        runtime = self._runtime.get(session_id)
        if not runtime or not runtime.output_buffer:
            return ""
        combined = "".join(runtime.output_buffer)
        runtime.output_buffer.clear()
        return combined

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
        runtime = self._runtime.get(session_id)
        if runtime:
            runtime.recent_output.clear()

    def get_recent_output(self, session_id: str) -> list[str]:
        """Get recent output chunks for a session.

        Args:
            session_id: Internal session identifier.

        Returns:
            List of recent output strings (up to 10).
        """
        runtime = self._runtime.get(session_id)
        return list(runtime.recent_output) if runtime else []

    def set_workdir(self, session_id: str, path: str, *, managed: bool) -> str:
        """Record a working directory and update the session metadata."""
        normalized = normalize_directory_path(path)
        session = self._sessions.get(session_id)
        if session:
            session.directory = normalized
            session.directory_has_git = has_git_repository(normalized)
            session.workdir_managed = managed
            self.update_session(session)
        return normalized

    def create_workdir(self, session_id: str) -> str:
        """Create a temporary working directory for the session."""
        path = tempfile.mkdtemp(prefix=f"tether_{session_id}_")
        return self.set_workdir(session_id, path, managed=True)

    def get_workdir(self, session_id: str) -> str | None:
        """Return the session working directory, if set."""
        session = self._sessions.get(session_id)
        return session.directory if session else None

    def clear_workdir(self, session_id: str, *, force: bool = True) -> None:
        """Clear the working directory, removing temp dirs if managed."""
        session = self._sessions.get(session_id)
        if not session:
            return
        if not force and not session.workdir_managed:
            return
        path = session.directory
        if path and session.workdir_managed:
            shutil.rmtree(path, ignore_errors=True)
        session.directory = None
        session.workdir_managed = False

    def add_message(self, session_id: str, role: str, content: object) -> Message:
        """Add a message to conversation history.

        Args:
            session_id: Internal session identifier.
            role: Message role ("user" or "assistant").
            content: Content blocks (will be JSON-encoded).
        """
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        now = self._now()
        content_json = json.dumps(content)
        with self._db_lock:
            with get_db_session() as db:
                max_seq = db.exec(
                    select(sa_func.coalesce(sa_func.max(Message.seq), 0)).where(
                        Message.session_id == session_id
                    )
                ).one()
                seq = max_seq + 1
                message = Message(
                    id=message_id,
                    session_id=session_id,
                    role=role,
                    content=content_json,
                    created_at=now,
                    seq=seq,
                )
                db.add(message)
                db.commit()
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
            with get_db_session() as db:
                rows = db.exec(
                    select(Message).where(Message.session_id == session_id).order_by(Message.seq)
                ).all()
                messages = []
                for row in rows:
                    content = json.loads(row.content) if row.content else []
                    messages.append({"role": row.role, "content": content})
                return messages

    def clear_messages(self, session_id: str) -> None:
        """Clear conversation history for a session.

        Args:
            session_id: Internal session identifier.
        """
        with self._db_lock:
            with get_db_session() as db:
                messages = db.exec(select(Message).where(Message.session_id == session_id)).all()
                for msg in messages:
                    db.delete(msg)
                db.commit()

    def get_message_count(self, session_id: str) -> int:
        """Get the number of conversation turns for a session.

        For attached external sessions, this returns the synced turn count
        (number of user messages) since those messages are emitted as events
        but not persisted to the DB.

        Args:
            session_id: Internal session identifier.

        Returns:
            Number of conversation turns in the session.
        """
        # Check synced turn count first (for attached external sessions)
        synced_turns = self.get_synced_turn_count(session_id)

        with self._db_lock:
            with get_db_session() as db:
                # Count only user messages for consistency with synced turn count
                db_count = db.exec(
                    select(sa_func.count(Message.id)).where(
                        Message.session_id == session_id,
                        Message.role == "user",
                    )
                ).one()
                db_count = db_count or 0

        # Return whichever is higher (synced turns for attached sessions,
        # db count for sessions that were started fresh)
        return max(synced_turns, db_count)

    def request_stop(self, session_id: str) -> None:
        """Signal the runner to stop."""
        self._get_runtime(session_id).stop_requested = True

    def is_stop_requested(self, session_id: str) -> bool:
        """Check if stop was requested for a session."""
        runtime = self._runtime.get(session_id)
        return runtime.stop_requested if runtime else False

    def clear_stop_requested(self, session_id: str) -> None:
        runtime = self._runtime.get(session_id)
        if runtime:
            runtime.stop_requested = False

    def add_pending_permission(
        self,
        session_id: str,
        request_id: str,
        tool_name: str,
        tool_input: dict,
        future: asyncio.Future,
    ) -> None:
        """Add a pending permission request waiting for user response.

        Args:
            session_id: Internal session identifier.
            request_id: Unique identifier for this permission request.
            tool_name: Name of the tool requesting permission.
            tool_input: Input parameters for the tool.
            future: Future to resolve when user responds.
        """
        runtime = self._get_runtime(session_id)
        runtime.pending_permissions[request_id] = PendingPermission(
            request_id=request_id,
            tool_name=tool_name,
            tool_input=tool_input,
            future=future,
        )

    def get_pending_permission(
        self, session_id: str, request_id: str
    ) -> PendingPermission | None:
        """Get a pending permission request by ID.

        Args:
            session_id: Internal session identifier.
            request_id: The permission request ID.

        Returns:
            The pending permission if found, None otherwise.
        """
        runtime = self._runtime.get(session_id)
        if not runtime:
            return None
        return runtime.pending_permissions.get(request_id)

    def get_all_pending_permissions(self, session_id: str) -> list[PendingPermission]:
        """Get all pending permission requests for a session.

        Args:
            session_id: Internal session identifier.

        Returns:
            List of pending permissions, empty if none.
        """
        runtime = self._runtime.get(session_id)
        if not runtime:
            return []
        return list(runtime.pending_permissions.values())

    def resolve_pending_permission(
        self, session_id: str, request_id: str, result: dict
    ) -> bool:
        """Resolve a pending permission request with the user's decision.

        Args:
            session_id: Internal session identifier.
            request_id: The permission request ID.
            result: The permission result dict (allow/deny with details).

        Returns:
            True if the permission was found and resolved, False otherwise.
        """
        runtime = self._runtime.get(session_id)
        if not runtime:
            return False
        pending = runtime.pending_permissions.pop(request_id, None)
        if not pending:
            return False
        if not pending.future.done():
            pending.future.set_result(result)
        return True

    def clear_pending_permissions(self, session_id: str) -> None:
        """Clear all pending permission requests for a session.

        Args:
            session_id: Internal session identifier.
        """
        runtime = self._runtime.get(session_id)
        if runtime:
            for pending in runtime.pending_permissions.values():
                if not pending.future.done():
                    pending.future.cancel()
            runtime.pending_permissions.clear()

    def read_event_log(
        self, session_id: str, *, since_seq: int = 0, limit: int | None = None
    ) -> list[dict]:
        """Read persisted SSE events for a session.

        Args:
            session_id: Internal session identifier.
            since_seq: Only return events with seq greater than this value.
            limit: Optional maximum number of events to return.
        """
        path = os.path.join(self._data_dir, "sessions", session_id, "events.jsonl")
        if not os.path.exists(path):
            return []
        events: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    seq = int(event.get("seq") or 0)
                    if seq and seq <= since_seq:
                        continue
                    events.append(event)
                    if limit and len(events) >= limit:
                        break
        except OSError:
            return []
        return events


    def session_usage(self, session_id: str) -> dict:
        """Aggregate token and cost usage from the event log.

        Returns dict with input_tokens, output_tokens, total_cost_usd.
        """
        input_tokens = 0
        output_tokens = 0
        total_cost = 0.0

        path = os.path.join(self._data_dir, "sessions", session_id, "events.jsonl")
        if not os.path.exists(path):
            return {"input_tokens": 0, "output_tokens": 0, "total_cost_usd": 0.0}

        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "metadata":
                        continue
                    data = event.get("data", {})
                    key = data.get("key")
                    value = data.get("value")
                    if key == "tokens" and isinstance(value, dict):
                        input_tokens += int(value.get("input", 0))
                        output_tokens += int(value.get("output", 0))
                    elif key == "cost" and isinstance(value, (int, float)):
                        total_cost += float(value)
        except OSError:
            pass

        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_cost_usd": round(total_cost, 4),
        }


store = SessionStore()
