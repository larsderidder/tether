"""Unit tests for session state transitions."""

import pytest
from fastapi import HTTPException

from tether.api.state import transition, _VALID_TRANSITIONS, maybe_set_session_name
from tether.models import SessionState
from tether.store import SessionStore


class TestValidTransitions:
    """Test that valid state transitions succeed."""

    def test_created_to_running(self, fresh_store: SessionStore) -> None:
        """CREATED -> RUNNING is valid."""
        session = fresh_store.create_session("repo_test", "main")
        assert session.state == SessionState.CREATED

        transition(session, SessionState.RUNNING, started_at=True)

        assert session.state == SessionState.RUNNING
        assert session.started_at is not None

    def test_running_to_awaiting_input(self, fresh_store: SessionStore) -> None:
        """RUNNING -> AWAITING_INPUT is valid."""
        session = fresh_store.create_session("repo_test", "main")
        transition(session, SessionState.RUNNING, started_at=True)

        transition(session, SessionState.AWAITING_INPUT)

        assert session.state == SessionState.AWAITING_INPUT

    def test_awaiting_input_to_running(self, fresh_store: SessionStore) -> None:
        """AWAITING_INPUT -> RUNNING is valid (user provides input)."""
        session = fresh_store.create_session("repo_test", "main")
        transition(session, SessionState.RUNNING, started_at=True)
        transition(session, SessionState.AWAITING_INPUT)

        transition(session, SessionState.RUNNING)

        assert session.state == SessionState.RUNNING

    def test_running_to_stopping(self, fresh_store: SessionStore) -> None:
        """RUNNING -> STOPPING is valid."""
        session = fresh_store.create_session("repo_test", "main")
        transition(session, SessionState.RUNNING, started_at=True)

        transition(session, SessionState.STOPPING)

        assert session.state == SessionState.STOPPING

    def test_running_to_stopped(self, fresh_store: SessionStore) -> None:
        """RUNNING -> STOPPED is valid (clean exit)."""
        session = fresh_store.create_session("repo_test", "main")
        transition(session, SessionState.RUNNING, started_at=True)

        transition(session, SessionState.STOPPED, ended_at=True)

        assert session.state == SessionState.STOPPED
        assert session.ended_at is not None

    def test_running_to_error(self, fresh_store: SessionStore) -> None:
        """RUNNING -> ERROR is valid (runner fails)."""
        session = fresh_store.create_session("repo_test", "main")
        transition(session, SessionState.RUNNING, started_at=True)

        transition(session, SessionState.ERROR, ended_at=True, exit_code=1)

        assert session.state == SessionState.ERROR
        assert session.exit_code == 1

    def test_stopping_to_stopped(self, fresh_store: SessionStore) -> None:
        """STOPPING -> STOPPED is valid."""
        session = fresh_store.create_session("repo_test", "main")
        transition(session, SessionState.RUNNING, started_at=True)
        transition(session, SessionState.STOPPING)

        transition(session, SessionState.STOPPED, ended_at=True)

        assert session.state == SessionState.STOPPED

    def test_stopped_to_running(self, fresh_store: SessionStore) -> None:
        """STOPPED -> RUNNING is valid (restart)."""
        session = fresh_store.create_session("repo_test", "main")
        transition(session, SessionState.RUNNING, started_at=True)
        transition(session, SessionState.STOPPED, ended_at=True)

        transition(session, SessionState.RUNNING)

        assert session.state == SessionState.RUNNING

    def test_error_to_running(self, fresh_store: SessionStore) -> None:
        """ERROR -> RUNNING is valid (retry after failure)."""
        session = fresh_store.create_session("repo_test", "main")
        transition(session, SessionState.RUNNING, started_at=True)
        transition(session, SessionState.ERROR, ended_at=True, exit_code=1)

        transition(session, SessionState.RUNNING)

        assert session.state == SessionState.RUNNING


class TestInvalidTransitions:
    """Test that invalid state transitions raise errors."""

    def test_created_to_stopped_invalid(self, fresh_store: SessionStore) -> None:
        """CREATED -> STOPPED is invalid (must run first)."""
        session = fresh_store.create_session("repo_test", "main")

        with pytest.raises(HTTPException) as exc_info:
            transition(session, SessionState.STOPPED)

        assert exc_info.value.status_code == 409

    def test_created_to_error_invalid(self, fresh_store: SessionStore) -> None:
        """CREATED -> ERROR is invalid."""
        session = fresh_store.create_session("repo_test", "main")

        with pytest.raises(HTTPException) as exc_info:
            transition(session, SessionState.ERROR)

        assert exc_info.value.status_code == 409

    def test_stopped_to_stopped_invalid(self, fresh_store: SessionStore) -> None:
        """STOPPED -> STOPPED is invalid (same state)."""
        session = fresh_store.create_session("repo_test", "main")
        transition(session, SessionState.RUNNING, started_at=True)
        transition(session, SessionState.STOPPED, ended_at=True)

        with pytest.raises(HTTPException) as exc_info:
            transition(session, SessionState.STOPPED)

        assert exc_info.value.status_code == 409

    def test_stopped_to_created_invalid(self, fresh_store: SessionStore) -> None:
        """STOPPED -> CREATED is invalid."""
        session = fresh_store.create_session("repo_test", "main")
        transition(session, SessionState.RUNNING, started_at=True)
        transition(session, SessionState.STOPPED, ended_at=True)

        with pytest.raises(HTTPException) as exc_info:
            transition(session, SessionState.CREATED)

        assert exc_info.value.status_code == 409

    def test_stopping_to_running_invalid(self, fresh_store: SessionStore) -> None:
        """STOPPING -> RUNNING is invalid (can't resume during stop)."""
        session = fresh_store.create_session("repo_test", "main")
        transition(session, SessionState.RUNNING, started_at=True)
        transition(session, SessionState.STOPPING)

        with pytest.raises(HTTPException) as exc_info:
            transition(session, SessionState.RUNNING)

        assert exc_info.value.status_code == 409


class TestTransitionOptions:
    """Test transition function options."""

    def test_allow_same_state(self, fresh_store: SessionStore) -> None:
        """allow_same=True permits no-op transitions."""
        session = fresh_store.create_session("repo_test", "main")
        transition(session, SessionState.RUNNING, started_at=True)

        # Should not raise
        transition(session, SessionState.RUNNING, allow_same=True)

        assert session.state == SessionState.RUNNING

    def test_timestamps_updated(self, fresh_store: SessionStore) -> None:
        """Transitions update last_activity_at."""
        session = fresh_store.create_session("repo_test", "main")
        original_activity = session.last_activity_at

        transition(session, SessionState.RUNNING, started_at=True)

        assert session.last_activity_at >= original_activity


class TestMaybeSetSessionName:
    """Test session name auto-population."""

    def test_sets_name_from_prompt(self, fresh_store: SessionStore) -> None:
        """Prompt sets session name when current name is default."""
        session = fresh_store.create_session("repo_test", "main")
        # Clear the default name to test the setting logic
        session.name = None
        fresh_store.update_session(session)

        maybe_set_session_name(session, "Fix the login bug")

        assert session.name == "Fix the login bug"

    def test_does_not_overwrite_existing_name(self, fresh_store: SessionStore) -> None:
        """Existing name is not overwritten."""
        session = fresh_store.create_session("repo_test", "main")
        session.name = "Original Name"
        fresh_store.update_session(session)

        maybe_set_session_name(session, "New Name")

        assert session.name == "Original Name"

    def test_truncates_long_name(self, fresh_store: SessionStore) -> None:
        """Long prompts are truncated to 80 chars."""
        session = fresh_store.create_session("repo_test", "main")
        # Clear the default name to test the setting logic
        session.name = None
        fresh_store.update_session(session)
        long_prompt = "x" * 100

        maybe_set_session_name(session, long_prompt)

        assert len(session.name) == 80

    def test_ignores_empty_prompt(self, fresh_store: SessionStore) -> None:
        """Empty prompts don't change name."""
        session = fresh_store.create_session("repo_test", "main")
        # Clear the default name to test the setting logic
        session.name = None
        fresh_store.update_session(session)

        maybe_set_session_name(session, "")
        maybe_set_session_name(session, "   ")

        assert session.name is None


class TestTransitionMatrix:
    """Verify the complete transition matrix is correct."""

    def test_all_states_have_transitions(self) -> None:
        """Every state has defined transitions."""
        for state in SessionState:
            assert state in _VALID_TRANSITIONS, f"Missing transitions for {state}"

    def test_transition_matrix_consistency(self) -> None:
        """The transition matrix matches expected behavior."""
        # CREATED can only go to RUNNING
        assert _VALID_TRANSITIONS[SessionState.CREATED] == {SessionState.RUNNING}

        # RUNNING can go to multiple states
        assert SessionState.AWAITING_INPUT in _VALID_TRANSITIONS[SessionState.RUNNING]
        assert SessionState.STOPPING in _VALID_TRANSITIONS[SessionState.RUNNING]
        assert SessionState.STOPPED in _VALID_TRANSITIONS[SessionState.RUNNING]
        assert SessionState.ERROR in _VALID_TRANSITIONS[SessionState.RUNNING]

        # Terminal states can restart
        assert SessionState.RUNNING in _VALID_TRANSITIONS[SessionState.STOPPED]
        assert SessionState.RUNNING in _VALID_TRANSITIONS[SessionState.ERROR]
