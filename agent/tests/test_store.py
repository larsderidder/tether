"""Unit tests for SessionStore operations."""

import os
import sqlite3

import pytest
from sqlmodel import select

from tether.db import get_session as get_db_session
from tether.models import BridgeDelivery, BridgeReplayCursor, SessionState
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

        fresh_store.clear_runner_session_id(session.id, force=True)
        assert fresh_store.get_runner_session_id(session.id) is None

    def test_update_session_cannot_change_runner_session_id(
        self, fresh_store: SessionStore
    ) -> None:
        """update_session cannot change or clear runner_session_id once set."""
        session = fresh_store.create_session("repo_test", "main")
        fresh_store.set_runner_session_id(session.id, "runner_123")

        # Attempt to clear via full session update.
        session.runner_session_id = None
        fresh_store.update_session(session)
        assert fresh_store.get_runner_session_id(session.id) == "runner_123"

        # Attempt to change to a different value via full session update.
        session.runner_session_id = "runner_999"
        fresh_store.update_session(session)
        assert fresh_store.get_runner_session_id(session.id) == "runner_123"

    def test_replace_runner_session_id_on_expiry(
        self, fresh_store: SessionStore
    ) -> None:
        """replace_runner_session_id updates binding when old session expired."""
        session = fresh_store.create_session("repo_test", "main")
        fresh_store.set_runner_session_id(session.id, "old_sdk_123")
        assert fresh_store.get_runner_session_id(session.id) == "old_sdk_123"

        # SDK expired and created a new session — replace should succeed
        fresh_store.replace_runner_session_id(session.id, "old_sdk_123", "new_sdk_456")
        assert fresh_store.get_runner_session_id(session.id) == "new_sdk_456"

    def test_replace_runner_session_id_wrong_old_id(
        self, fresh_store: SessionStore
    ) -> None:
        """replace_runner_session_id rejects if old_id doesn't match current."""
        session = fresh_store.create_session("repo_test", "main")
        fresh_store.set_runner_session_id(session.id, "current_abc")

        fresh_store.replace_runner_session_id(session.id, "wrong_old", "new_xyz")
        assert fresh_store.get_runner_session_id(session.id) == "current_abc"

    def test_replace_runner_session_id_not_stolen(
        self, fresh_store: SessionStore
    ) -> None:
        """replace_runner_session_id won't steal an ID from another session."""
        session_a = fresh_store.create_session("repo_a", "main")
        session_b = fresh_store.create_session("repo_b", "main")
        fresh_store.set_runner_session_id(session_a.id, "sdk_aaa")
        fresh_store.set_runner_session_id(session_b.id, "sdk_bbb")

        # Try to replace B's binding with A's SDK session ID
        fresh_store.replace_runner_session_id(session_b.id, "sdk_bbb", "sdk_aaa")
        assert fresh_store.get_runner_session_id(session_b.id) == "sdk_bbb"

    def test_synced_message_counts_survive_restart(
        self, fresh_store: SessionStore
    ) -> None:
        """External transcript cursors persist across store reconstruction."""
        session = fresh_store.create_session("repo_test", "main")
        fresh_store.set_synced_message_count(session.id, 42, 7)

        restarted_store = SessionStore()

        assert restarted_store.get_synced_message_count(session.id) == 42
        assert restarted_store.get_synced_turn_count(session.id) == 7

    def test_stop_requested(self, fresh_store: SessionStore) -> None:
        """Stop request flag can be set and checked."""
        session = fresh_store.create_session("repo_test", "main")

        assert fresh_store.is_stop_requested(session.id) is False

        fresh_store.request_stop(session.id)
        assert fresh_store.is_stop_requested(session.id) is True

        fresh_store.clear_stop_requested(session.id)
        assert fresh_store.is_stop_requested(session.id) is False


class TestBridgeDeliveryOutbox:
    """Test durable bridge delivery ordering and cleanup."""

    def test_failed_head_blocks_later_delivery(self, fresh_store: SessionStore) -> None:
        """A later due row cannot overtake an earlier row in retry backoff."""
        session = fresh_store.create_session("repo_test", "main")
        first = fresh_store.enqueue_bridge_delivery(
            session_id=session.id,
            platform="telegram",
            delivery_key="first",
            method="on_output",
            payload={"text": "first"},
        )
        second = fresh_store.enqueue_bridge_delivery(
            session_id=session.id,
            platform="telegram",
            delivery_key="second",
            method="on_output",
            payload={"text": "second"},
        )

        claimed = fresh_store.claim_pending_bridge_deliveries(session.id, "telegram")
        assert [row.id for row in claimed] == [first.id]
        fresh_store.mark_bridge_delivery_failed(
            first.id, RuntimeError("offline"), retry_delay_s=60
        )

        assert fresh_store.claim_pending_bridge_deliveries(session.id, "telegram") == []
        assert fresh_store.claim_bridge_delivery(second.id) is None

    def test_active_head_blocks_later_delivery_across_store_instances(
        self, fresh_store: SessionStore
    ) -> None:
        """A second store instance cannot claim behind an active FIFO head."""
        session = fresh_store.create_session("repo_test", "main")
        first = fresh_store.enqueue_bridge_delivery(
            session_id=session.id,
            platform="telegram",
            delivery_key="first-active",
            method="on_output",
            payload={"text": "first"},
        )
        second = fresh_store.enqueue_bridge_delivery(
            session_id=session.id,
            platform="telegram",
            delivery_key="second-active",
            method="on_output",
            payload={"text": "second"},
        )

        claimed = fresh_store.claim_pending_bridge_deliveries(session.id, "telegram")
        assert [row.id for row in claimed] == [first.id]
        reloaded_store = SessionStore()

        assert (
            reloaded_store.claim_pending_bridge_deliveries(session.id, "telegram") == []
        )
        assert reloaded_store.claim_bridge_delivery(second.id) is None

    def test_session_delete_removes_bridge_delivery_and_cursor_rows(
        self, fresh_store: SessionStore
    ) -> None:
        """Deleting a session removes durable bridge rows even without SQLite FK enforcement."""
        session = fresh_store.create_session("repo_test", "main")
        fresh_store.enqueue_bridge_delivery(
            session_id=session.id,
            platform="telegram",
            delivery_key="cleanup",
            method="on_output",
            payload={"text": "cleanup"},
        )
        fresh_store.bridge_replay_cursor(session.id, "telegram")

        assert fresh_store.delete_session(session.id) is True

        with get_db_session() as db:
            deliveries = db.exec(select(BridgeDelivery)).all()
            cursors = db.exec(select(BridgeReplayCursor)).all()
        assert deliveries == []
        assert cursors == []


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


class TestEventLogDelivery:
    """Test durable event persistence with bounded live queues."""

    @pytest.mark.anyio
    async def test_emit_persists_when_subscriber_queue_is_full(
        self, fresh_store: SessionStore
    ) -> None:
        """A saturated live queue must not block durable event persistence."""
        session = fresh_store.create_session("repo_test", "main")
        queue = fresh_store.new_subscriber(session.id)
        for index in range(queue.maxsize):
            queue.put_nowait({"type": "heartbeat", "seq": index + 1, "data": {}})

        event = {
            "session_id": session.id,
            "seq": 2000,
            "type": "output",
            "data": {"text": "kept"},
        }
        await fresh_store.emit(session.id, event)

        assert fresh_store.read_event_log(session.id, since_seq=1999) == [event]
        fresh_store.remove_subscriber(session.id, queue)

    def test_rotated_event_log_replay_and_usage(
        self, fresh_store: SessionStore
    ) -> None:
        """Readers include the .1 rotated log before the active log."""
        session = fresh_store.create_session("repo_test", "main")
        fresh_store._append_event_log(
            session.id,
            {
                "session_id": session.id,
                "seq": 1,
                "type": "metadata",
                "data": {"key": "tokens", "value": {"input": 10, "output": 3}},
            },
        )
        active = os.path.join(
            fresh_store._data_dir, "sessions", session.id, "events.jsonl"
        )
        os.replace(active, f"{active}.1")
        fresh_store._append_event_log(
            session.id,
            {
                "session_id": session.id,
                "seq": 2,
                "type": "metadata",
                "data": {"key": "cost", "value": 0.125},
            },
        )

        assert [event["seq"] for event in fresh_store.read_event_log(session.id)] == [
            1,
            2,
        ]
        assert fresh_store.session_usage(session.id) == {
            "input_tokens": 10,
            "output_tokens": 3,
            "total_cost_usd": 0.125,
        }


class TestMigrationEnforcement:
    """Test that init_db runs migrations to update existing schemas."""

    def test_migration_adds_missing_columns(self, tmp_path, monkeypatch) -> None:
        """An old DB missing columns gets them added via migration."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)
        db_path = os.path.join(data_dir, "sessions.db")

        # Create a minimal DB with only the initial schema columns
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE sessions (
                id VARCHAR PRIMARY KEY,
                repo_id VARCHAR NOT NULL,
                repo_display VARCHAR NOT NULL,
                repo_ref_type VARCHAR NOT NULL,
                repo_ref_value VARCHAR NOT NULL,
                state VARCHAR NOT NULL,
                name VARCHAR,
                created_at VARCHAR NOT NULL,
                started_at VARCHAR,
                ended_at VARCHAR,
                last_activity_at VARCHAR NOT NULL,
                exit_code INTEGER,
                summary VARCHAR,
                runner_header VARCHAR,
                runner_type VARCHAR,
                runner_session_id VARCHAR UNIQUE,
                directory VARCHAR,
                directory_has_git BOOLEAN NOT NULL DEFAULT 0,
                workdir_managed BOOLEAN NOT NULL DEFAULT 0
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE messages (
                id VARCHAR PRIMARY KEY,
                session_id VARCHAR NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role VARCHAR NOT NULL,
                content VARCHAR,
                created_at VARCHAR NOT NULL,
                seq INTEGER NOT NULL
            )
        """
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", data_dir)

        from tether.db import reset_engine, init_db

        reset_engine()
        init_db()

        # Verify the new columns exist
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        # These columns were added by later migrations
        assert "adapter" in columns, "adapter column missing after migration"
        assert (
            "approval_mode" in columns
        ), "approval_mode column missing after migration"
        assert "platform" in columns, "platform column missing after migration"
        assert (
            "external_agent_name" in columns
        ), "external_agent_name column missing after migration"
        assert (
            "bridge_verbosity" in columns
        ), "bridge_verbosity column missing after migration"
        assert (
            "bridge_buffer_max_seconds" in columns
        ), "bridge_buffer_max_seconds column missing after migration"
