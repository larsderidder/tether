"""Unit tests for SessionStore operations."""

import pytest

from tether.models import SessionState
from tether.store import SessionStore


class TestSessionCRUD:
    """Test basic session create, read, update, delete operations."""

    def test_create_session(self, fresh_store: SessionStore) -> None:
        """Creating a session returns a valid session in CREATED state."""
        session = fresh_store.create_session("repo_test", "main")

        assert session.id.startswith("sess_")
        assert session.repo_id == "repo_test"
        assert session.state == SessionState.CREATED
        assert session.created_at is not None
        assert session.started_at is None
        assert session.ended_at is None

    def test_get_session(self, fresh_store: SessionStore) -> None:
        """Getting a session by ID returns the same session."""
        created = fresh_store.create_session("repo_test", "main")
        retrieved = fresh_store.get_session(created.id)

        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.repo_id == created.repo_id

    def test_get_nonexistent_session(self, fresh_store: SessionStore) -> None:
        """Getting a nonexistent session returns None."""
        result = fresh_store.get_session("nonexistent_id")
        assert result is None

    def test_list_sessions(self, fresh_store: SessionStore) -> None:
        """Listing sessions returns all created sessions."""
        s1 = fresh_store.create_session("repo_1", "main")
        s2 = fresh_store.create_session("repo_2", "main")

        sessions = fresh_store.list_sessions()
        session_ids = {s.id for s in sessions}

        assert s1.id in session_ids
        assert s2.id in session_ids

    def test_update_session(self, fresh_store: SessionStore) -> None:
        """Updating a session persists changes."""
        session = fresh_store.create_session("repo_test", "main")
        session.name = "Updated Name"
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        retrieved = fresh_store.get_session(session.id)
        assert retrieved is not None
        assert retrieved.name == "Updated Name"
        assert retrieved.state == SessionState.RUNNING

    def test_delete_session(self, fresh_store: SessionStore) -> None:
        """Deleting a session removes it from the store."""
        session = fresh_store.create_session("repo_test", "main")
        session_id = session.id

        fresh_store.delete_session(session_id)
        result = fresh_store.get_session(session_id)

        assert result is None


class TestSessionDirectory:
    """Test directory-related session operations."""

    def test_set_session_directory(self, fresh_store: SessionStore, tmp_path) -> None:
        """Setting workdir updates the session directory."""
        test_dir = str(tmp_path / "test_repo")
        import os
        os.makedirs(test_dir, exist_ok=True)

        session = fresh_store.create_session("repo_test", "main")
        fresh_store.set_workdir(session.id, test_dir, managed=False)

        workdir = fresh_store.get_workdir(session.id)
        assert workdir == test_dir

    def test_set_workdir(self, fresh_store: SessionStore, tmp_path) -> None:
        """Setting workdir records the path."""
        session = fresh_store.create_session("repo_test", "main")
        workdir = str(tmp_path / "workdir")
        import os
        os.makedirs(workdir, exist_ok=True)

        fresh_store.set_workdir(session.id, workdir, managed=False)
        result = fresh_store.get_workdir(session.id)

        assert result == workdir


class TestProcessBookkeeping:
    """Test process and runtime state management."""

    def test_pending_inputs(self, fresh_store: SessionStore) -> None:
        """Pending inputs queue works correctly."""
        session = fresh_store.create_session("repo_test", "main")

        fresh_store.add_pending_input(session.id, "input1")
        fresh_store.add_pending_input(session.id, "input2")

        first = fresh_store.pop_next_pending_input(session.id)
        second = fresh_store.pop_next_pending_input(session.id)
        third = fresh_store.pop_next_pending_input(session.id)

        assert first == "input1"
        assert second == "input2"
        assert third is None

    def test_runner_session_id(self, fresh_store: SessionStore) -> None:
        """Runner session ID can be set and retrieved."""
        session = fresh_store.create_session("repo_test", "main")

        assert fresh_store.get_runner_session_id(session.id) is None

        fresh_store.set_runner_session_id(session.id, "runner_123")
        assert fresh_store.get_runner_session_id(session.id) == "runner_123"

        fresh_store.clear_runner_session_id(session.id)
        assert fresh_store.get_runner_session_id(session.id) is None

    def test_stop_requested(self, fresh_store: SessionStore) -> None:
        """Stop request flag can be set and checked."""
        session = fresh_store.create_session("repo_test", "main")

        assert fresh_store.is_stop_requested(session.id) is False

        fresh_store.request_stop(session.id)
        assert fresh_store.is_stop_requested(session.id) is True

        fresh_store.clear_stop_requested(session.id)
        assert fresh_store.is_stop_requested(session.id) is False


class TestEventSequence:
    """Test event sequence number management."""

    def test_next_seq_increments(self, fresh_store: SessionStore) -> None:
        """Sequence numbers increment for each call."""
        session = fresh_store.create_session("repo_test", "main")

        seq1 = fresh_store.next_seq(session.id)
        seq2 = fresh_store.next_seq(session.id)
        seq3 = fresh_store.next_seq(session.id)

        assert seq1 == 1
        assert seq2 == 2
        assert seq3 == 3

    def test_seq_isolated_per_session(self, fresh_store: SessionStore) -> None:
        """Each session has its own sequence counter."""
        s1 = fresh_store.create_session("repo_1", "main")
        s2 = fresh_store.create_session("repo_2", "main")

        fresh_store.next_seq(s1.id)
        fresh_store.next_seq(s1.id)
        seq_s1 = fresh_store.next_seq(s1.id)

        seq_s2 = fresh_store.next_seq(s2.id)

        assert seq_s1 == 3
        assert seq_s2 == 1
