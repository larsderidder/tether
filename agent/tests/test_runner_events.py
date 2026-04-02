"""Tests for ApiRunnerEvents callbacks bridging runner → SSE events."""

import pytest

from tether.models import SessionState
from tether.store import SessionStore


class TestOnOutput:
    """Test ApiRunnerEvents.on_output callback."""

    @pytest.mark.anyio
    async def test_output_updates_activity_timestamp(
        self, fresh_store: SessionStore
    ) -> None:
        """on_output updates session.last_activity_at."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        session.last_activity_at = "2020-01-01T00:00:00Z"
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_output(session.id, "combined", "hello", kind="step")

        updated = fresh_store.get_session(session.id)
        assert updated.last_activity_at != "2020-01-01T00:00:00Z"

    @pytest.mark.anyio
    async def test_output_header_kind_stores_runner_header(
        self, fresh_store: SessionStore
    ) -> None:
        """on_output with kind='header' stores text as runner_header."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_output(
            session.id, "combined", "Claude Code v1.0", kind="header"
        )

        updated = fresh_store.get_session(session.id)
        assert updated.runner_header == "Claude Code v1.0"

    @pytest.mark.anyio
    async def test_output_missing_session_noop(self, fresh_store: SessionStore) -> None:
        """on_output with unknown session_id is a no-op."""
        from tether.api.runner_events import ApiRunnerEvents

        events = ApiRunnerEvents()
        # Should not raise
        await events.on_output("nonexistent", "combined", "hello")

    @pytest.mark.anyio
    async def test_final_output_is_held_until_terminal_state(
        self, fresh_store: SessionStore, tmp_path
    ) -> None:
        """Final output is postprocessed and deferred until finalization."""
        from tether.api.runner_events import ApiRunnerEvents

        report = tmp_path / "report.txt"
        report.write_text("artifact", encoding="utf-8")

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        session.directory = str(tmp_path)
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_output(
            session.id,
            "combined",
            "Done.\nPUBLISH AS ATTACHEMENT: report.txt",
            kind="final",
            is_final=True,
        )

        log = fresh_store.read_event_log(session.id)
        assert [event for event in log if event.get("type") == "output"] == []

        pending_text, attachments, warnings = fresh_store.get_pending_final_output(
            session.id
        )
        assert pending_text == "Done."
        assert warnings == []
        assert attachments[0]["filename"] == "report.txt"


class TestOnHeader:
    """Test ApiRunnerEvents.on_header callback."""

    @pytest.mark.anyio
    async def test_header_stores_title(self, fresh_store: SessionStore) -> None:
        """on_header stores title as runner_header."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        events = ApiRunnerEvents()

        await events.on_header(session.id, title="Claude Code 1.0.3")

        updated = fresh_store.get_session(session.id)
        assert updated.runner_header == "Claude Code 1.0.3"

    @pytest.mark.anyio
    async def test_header_captures_thread_id(self, fresh_store: SessionStore) -> None:
        """on_header stores thread_id as runner_session_id."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        events = ApiRunnerEvents()

        await events.on_header(session.id, title="test", thread_id="thread_abc123")

        assert fresh_store.get_runner_session_id(session.id) == "thread_abc123"

    @pytest.mark.anyio
    async def test_header_does_not_overwrite_existing_thread_id(
        self, fresh_store: SessionStore
    ) -> None:
        """on_header does not overwrite an existing runner_session_id."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        fresh_store.set_runner_session_id(session.id, "original_thread")
        events = ApiRunnerEvents()

        await events.on_header(session.id, title="test", thread_id="new_thread")

        assert fresh_store.get_runner_session_id(session.id) == "original_thread"

    @pytest.mark.anyio
    async def test_header_ignores_unknown_thread_id(
        self, fresh_store: SessionStore
    ) -> None:
        """on_header ignores thread_id='unknown'."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        events = ApiRunnerEvents()

        await events.on_header(session.id, title="test", thread_id="unknown")

        assert fresh_store.get_runner_session_id(session.id) is None


class TestOnError:
    """Test ApiRunnerEvents.on_error callback."""

    @pytest.mark.anyio
    async def test_error_transitions_to_error_state(
        self, fresh_store: SessionStore
    ) -> None:
        """on_error transitions session to ERROR state."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_error(session.id, "CRASH", "Process died")

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.ERROR

    @pytest.mark.anyio
    async def test_error_idempotent_if_already_error(
        self, fresh_store: SessionStore
    ) -> None:
        """on_error does not re-transition if already in ERROR."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.ERROR
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        # Should not raise or change state
        await events.on_error(session.id, "CRASH", "Another error")

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.ERROR


class TestOnExit:
    """Test ApiRunnerEvents.on_exit callback."""

    @pytest.mark.anyio
    async def test_exit_zero_is_noop_when_running(
        self, fresh_store: SessionStore
    ) -> None:
        """on_exit with code 0 does NOT transition to error."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_exit(session.id, 0)

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.RUNNING

    @pytest.mark.anyio
    async def test_exit_none_is_noop(self, fresh_store: SessionStore) -> None:
        """on_exit with None exit_code is a no-op."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_exit(session.id, None)

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.RUNNING

    @pytest.mark.anyio
    async def test_exit_nonzero_transitions_to_error(
        self, fresh_store: SessionStore
    ) -> None:
        """on_exit with non-zero code transitions to ERROR."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_exit(session.id, 1)

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.ERROR

        log = fresh_store.read_event_log(session.id)
        error_events = [event for event in log if event.get("type") == "error"]
        assert error_events
        assert error_events[-1]["data"]["code"] == "RUNNER_EXIT"

    @pytest.mark.anyio
    async def test_exit_skipped_if_awaiting_input(
        self, fresh_store: SessionStore
    ) -> None:
        """on_exit is no-op if session already in AWAITING_INPUT."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.AWAITING_INPUT
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_exit(session.id, 1)

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.AWAITING_INPUT

    @pytest.mark.anyio
    async def test_exit_skipped_if_interrupting(
        self, fresh_store: SessionStore
    ) -> None:
        """on_exit is no-op if session in INTERRUPTING state."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)
        # Transition through RUNNING → INTERRUPTING
        from tether.api.state import transition

        transition(session, SessionState.INTERRUPTING)

        events = ApiRunnerEvents()
        await events.on_exit(session.id, 1)

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.INTERRUPTING


class TestOnAwaitingInput:
    """Test ApiRunnerEvents.on_awaiting_input callback."""

    @pytest.mark.anyio
    async def test_transitions_to_awaiting_input(
        self, fresh_store: SessionStore
    ) -> None:
        """on_awaiting_input transitions from RUNNING to AWAITING_INPUT."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_awaiting_input(session.id)

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.AWAITING_INPUT

    @pytest.mark.anyio
    async def test_idempotent_if_already_awaiting(
        self, fresh_store: SessionStore
    ) -> None:
        """on_awaiting_input is no-op if already AWAITING_INPUT."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.AWAITING_INPUT
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_awaiting_input(session.id)

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.AWAITING_INPUT

    @pytest.mark.anyio
    async def test_skipped_if_error(self, fresh_store: SessionStore) -> None:
        """on_awaiting_input is no-op if session in ERROR."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.ERROR
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_awaiting_input(session.id)

        updated = fresh_store.get_session(session.id)
        assert updated.state == SessionState.ERROR

    @pytest.mark.anyio
    async def test_finalizes_pending_output_with_stop_footer(
        self, fresh_store: SessionStore, tmp_path
    ) -> None:
        """Awaiting-input finalization appends the STOP footer and attachments."""
        from tether.api.runner_events import ApiRunnerEvents

        report = tmp_path / "report.txt"
        report.write_text("artifact", encoding="utf-8")

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        session.directory = str(tmp_path)
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_output(
            session.id,
            "combined",
            "Done.\nPUBLISH AS ATTACHMENT: report.txt",
            kind="final",
            is_final=True,
        )
        await events.on_metadata(session.id, "duration_ms", 12345, "12345")
        await events.on_awaiting_input(session.id)

        log = fresh_store.read_event_log(session.id)
        output_events = [event for event in log if event.get("type") == "output"]
        assert output_events
        assert output_events[-1]["data"]["text"] == "Done.\nSTOP 🛑✅ 12s"
        assert output_events[-1]["data"]["attachments"][0]["filename"] == "report.txt"

        output_final_events = [
            event for event in log if event.get("type") == "output_final"
        ]
        assert output_final_events
        assert output_final_events[-1]["data"]["text"] == "Done.\nSTOP 🛑✅ 12s"

    @pytest.mark.anyio
    async def test_error_finalizes_pending_output_with_error_footer(
        self, fresh_store: SessionStore
    ) -> None:
        """Errors finalize any pending output with the error STOP footer."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_output(
            session.id,
            "combined",
            "Partial final",
            kind="final",
            is_final=True,
        )
        await events.on_metadata(session.id, "duration_ms", 5000, "5000")
        await events.on_error(session.id, "CRASH", "Process died")

        log = fresh_store.read_event_log(session.id)
        output_events = [event for event in log if event.get("type") == "output"]
        assert output_events[-1]["data"]["text"] == "Partial final\nSTOP 🛑❌ 5s"


class TestOnMetadata:
    """Test ApiRunnerEvents.on_metadata callback."""

    @pytest.mark.anyio
    async def test_metadata_updates_activity(self, fresh_store: SessionStore) -> None:
        """on_metadata updates last_activity_at."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        session.last_activity_at = "2020-01-01T00:00:00Z"
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_metadata(session.id, "tokens", {"input": 100}, "raw text")

        updated = fresh_store.get_session(session.id)
        assert updated.last_activity_at != "2020-01-01T00:00:00Z"

    @pytest.mark.anyio
    async def test_metadata_emits_event(self, fresh_store: SessionStore) -> None:
        """on_metadata emits a metadata event to the store."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_metadata(
            session.id, "model", "claude-3.5-sonnet", "model: claude-3.5-sonnet"
        )

        log = fresh_store.read_event_log(session.id)
        metadata_events = [e for e in log if e.get("type") == "metadata"]
        assert len(metadata_events) >= 1
        assert metadata_events[-1]["data"]["key"] == "model"


class TestOnHeartbeat:
    """Test ApiRunnerEvents.on_heartbeat callback."""

    @pytest.mark.anyio
    async def test_heartbeat_updates_activity(self, fresh_store: SessionStore) -> None:
        """on_heartbeat updates last_activity_at."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_heartbeat(session.id, 30.0, False)

        updated = fresh_store.get_session(session.id)
        assert updated.last_activity_at is not None

    @pytest.mark.anyio
    async def test_heartbeat_emits_event(self, fresh_store: SessionStore) -> None:
        """on_heartbeat emits a heartbeat event to the store."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_heartbeat(session.id, 45.5, True)

        log = fresh_store.read_event_log(session.id)
        hb_events = [e for e in log if e.get("type") == "heartbeat"]
        assert len(hb_events) >= 1
        assert hb_events[-1]["data"]["elapsed_s"] == 45.5
        assert hb_events[-1]["data"]["done"] is True


class TestOnPermissionRequest:
    """Test ApiRunnerEvents.on_permission_request callback."""

    @pytest.mark.anyio
    async def test_permission_request_emits_event(
        self, fresh_store: SessionStore
    ) -> None:
        """on_permission_request emits permission_request event."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_permission_request(
            session.id, "perm_1", "Read", {"path": "/tmp/file.txt"}
        )

        log = fresh_store.read_event_log(session.id)
        perm_events = [e for e in log if e.get("type") == "permission_request"]
        assert len(perm_events) >= 1
        assert perm_events[-1]["data"]["request_id"] == "perm_1"
        assert perm_events[-1]["data"]["tool_name"] == "Read"

    @pytest.mark.anyio
    async def test_permission_request_missing_session_noop(
        self, fresh_store: SessionStore
    ) -> None:
        """on_permission_request with unknown session is a no-op."""
        from tether.api.runner_events import ApiRunnerEvents

        events = ApiRunnerEvents()
        await events.on_permission_request("nonexistent", "p1", "Read", {})


class TestOnPermissionResolved:
    """Test ApiRunnerEvents.on_permission_resolved callback."""

    @pytest.mark.anyio
    async def test_permission_resolved_emits_event(
        self, fresh_store: SessionStore
    ) -> None:
        """on_permission_resolved emits permission_resolved event."""
        from tether.api.runner_events import ApiRunnerEvents

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        events = ApiRunnerEvents()
        await events.on_permission_resolved(
            session.id, "perm_1", "user", True, "Approved by admin"
        )

        log = fresh_store.read_event_log(session.id)
        resolved_events = [e for e in log if e.get("type") == "permission_resolved"]
        assert len(resolved_events) >= 1
        assert resolved_events[-1]["data"]["request_id"] == "perm_1"
        assert resolved_events[-1]["data"]["allowed"] is True
        assert resolved_events[-1]["data"]["resolved_by"] == "user"
