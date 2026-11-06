"""Model-based checks for transcript cursors, state transitions and final output."""

import asyncio
import json
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from typing import Iterator

import pytest
from agent_sessions import RunnerType, SessionDetail, SessionMessage
from fastapi import HTTPException
from hypothesis import given, settings, strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)
from sqlmodel import select

from tether.api.emit import emit_output, finalize_output
from tether.api.state import transition
from tether.db import get_session as get_db_session, init_db, reset_engine
from tether.models import BridgeDelivery, SessionState
from tether.store import SessionStore
from tether.external_sync import sync_external_session_delta


@contextmanager
def isolated_store() -> Iterator[tuple[SessionStore, pytest.MonkeyPatch]]:
    """Give each generated sequence its own real database and event log."""
    with TemporaryDirectory(prefix="tether-property-") as directory:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("TETHER_AGENT_DATA_DIR", directory)
            reset_engine()
            init_db()
            store = SessionStore()
            for target in (
                "tether.store.store",
                "tether.external_sync.store",
                "tether.api.state.store",
                "tether.api.emit.store",
                "tether.api.runner_events.store",
            ):
                patch.setattr(target, store)
            try:
                yield store, patch
            finally:
                reset_engine()


class TranscriptCursorMachine(RuleBasedStateMachine):
    """Compare polling and restart sequences against an append-only transcript."""

    def __init__(self) -> None:
        super().__init__()
        self.isolation = isolated_store()
        self.store, self.patch = self.isolation.__enter__()
        self.session = self.store.create_session("external", None)
        self.session.adapter = "pi_rpc"
        self.session.runner_type = "pi"
        self.session.state = SessionState.AWAITING_INPUT
        self.store.update_session(self.session)
        self.store.set_runner_session_id(self.session.id, "property-transcript")
        self.messages: list[SessionMessage] = []
        self.baseline = 0
        self.cursor = 0
        self.patch.setattr(
            "tether.external_sync.get_external_session_detail", self.detail
        )

    def detail(
        self, session_id: str, runner_type: RunnerType, limit: int
    ) -> SessionDetail:
        """Emulate a provider's tail limit while retaining a complete model."""
        return SessionDetail(
            id=session_id,
            runner_type=runner_type,
            directory="/tmp/repo",
            first_prompt="hello",
            last_prompt="hello",
            last_activity="2026-01-01T00:00:00Z",
            is_running=False,
            message_count=min(len(self.messages), limit),
            messages=self.messages[-limit:],
        )

    @initialize(baseline=st.integers(min_value=0, max_value=1200))
    def attach(self, baseline: int) -> None:
        """Already displayed history must not be imported by later polling."""
        self.append(baseline)
        self.baseline = baseline
        self.cursor = baseline
        self.store.set_synced_message_count(self.session.id, baseline, 0)

    @rule(amount=st.integers(min_value=0, max_value=600))
    def append(self, amount: int) -> None:
        """Grow the independent transcript without changing Tether's cursor."""
        for index in range(len(self.messages), len(self.messages) + amount):
            self.messages.append(
                SessionMessage(role="assistant", content=f"answer-{index}")
            )

    @rule()
    def sync(self) -> None:
        """Every unseen message is imported once, regardless of transcript size."""
        result = asyncio.run(sync_external_session_delta(self.session.id))
        assert result.synced == len(self.messages) - self.cursor
        self.cursor = len(self.messages)

    @rule()
    def restart(self) -> None:
        """Reloading volatile state must not change the durable polling position."""
        self.store = SessionStore()
        for target in (
            "tether.external_sync.store",
            "tether.api.emit.store",
            "tether.api.state.store",
        ):
            self.patch.setattr(target, self.store)

    @invariant()
    def imported_history_matches_model(self) -> None:
        """Compare real stored output with the model, not with another cursor slice."""
        imported = [
            event["data"]["text"]
            for event in self.store.read_event_log(self.session.id)
            if event["type"] == "output"
        ]
        assert imported == [
            f"answer-{index}" for index in range(self.baseline, self.cursor)
        ]
        assert self.store.get_synced_message_count(self.session.id) == self.cursor

    def teardown(self) -> None:
        """Dispose generated storage even after a failing invariant."""
        self.isolation.__exit__(None, None, None)


class SessionTransitionMachine(RuleBasedStateMachine):
    """Attempt arbitrary transitions against the documented lifecycle contract."""

    def __init__(self) -> None:
        super().__init__()
        self.isolation = isolated_store()
        self.store, _ = self.isolation.__enter__()
        self.session = self.store.create_session("test", None)
        self.expected = SessionState.CREATED
        self.allowed = {
            SessionState.CREATED: {SessionState.RUNNING},
            SessionState.RUNNING: {
                SessionState.AWAITING_INPUT,
                SessionState.INTERRUPTING,
                SessionState.ERROR,
            },
            SessionState.AWAITING_INPUT: {SessionState.RUNNING, SessionState.ERROR},
            SessionState.INTERRUPTING: {
                SessionState.AWAITING_INPUT,
                SessionState.ERROR,
            },
            SessionState.ERROR: {SessionState.RUNNING},
        }

    @rule(new_state=st.sampled_from(tuple(SessionState)), allow_same=st.booleans())
    def attempt(self, new_state: SessionState, allow_same: bool) -> None:
        """Illegal transitions leave both the model and persisted session unchanged."""
        valid = new_state in self.allowed[self.expected] or (
            allow_same and new_state == self.expected
        )
        if valid:
            transition(self.session, new_state, allow_same=allow_same)
            self.expected = new_state
        else:
            before = self.session.model_dump()
            with pytest.raises(HTTPException) as error:
                transition(self.session, new_state, allow_same=allow_same)
            assert error.value.status_code == 409
            assert self.session.model_dump() == before

    @invariant()
    def persisted_state_matches_model(self) -> None:
        """A rejected transition must not leak into durable state."""
        assert self.store.get_session(self.session.id).state == self.expected

    def teardown(self) -> None:
        """Dispose generated storage even after a failing invariant."""
        self.isolation.__exit__(None, None, None)


TestTranscriptCursor = TranscriptCursorMachine.TestCase
TestTranscriptCursor.settings = settings(
    max_examples=25,
    stateful_step_count=12,
    deadline=None,
    database=None,
    derandomize=True,
)
TestSessionTransitions = SessionTransitionMachine.TestCase
TestSessionTransitions.settings = settings(
    max_examples=40,
    stateful_step_count=25,
    deadline=None,
    database=None,
    derandomize=True,
)


@settings(max_examples=40, deadline=None, database=None, derandomize=True)
@given(
    chunks=st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz .!?", min_size=1, max_size=20),
        min_size=1,
        max_size=8,
    )
)
def test_streamed_final_is_published_once(chunks: list[str]) -> None:
    """Streaming fragments and repeated completion never duplicate a final answer."""
    answer = "".join(chunks).strip()
    with isolated_store() as (store, _):
        session = store.create_session("test", None)

        async def run_turn() -> None:
            """Feed actual output aggregation with streamed and completed text."""
            for chunk in chunks:
                await emit_output(session, chunk, kind="step", is_final=False)
            await emit_output(session, answer, kind="final", is_final=True)
            await finalize_output(session, status="success")
            await finalize_output(session, status="success")

        asyncio.run(run_turn())
        finals = [
            event["data"]["text"]
            for event in store.read_event_log(session.id)
            if event["type"] == "output_final"
        ]
        assert finals == ([answer] if answer else [])


class BridgeOutboxMachine(RuleBasedStateMachine):
    """Compare deduplication, claims and store reloads with a FIFO delivery model."""

    def __init__(self) -> None:
        super().__init__()
        self.isolation = isolated_store()
        self.store, _ = self.isolation.__enter__()
        self.session = self.store.create_session("test", None)
        self.ids: dict[int, str] = {}
        self.states: dict[int, str] = {}
        self.active: int | None = None

    @initialize()
    def initial_delivery(self) -> None:
        """Every generated run starts with real pending work."""
        self.enqueue(0)

    @rule(key=st.integers(min_value=0, max_value=6))
    def enqueue(self, key: int) -> None:
        """Replaying a delivery key must never create or reactivate another row."""
        row = self.store.enqueue_bridge_delivery(
            session_id=self.session.id,
            platform="telegram",
            delivery_key=f"model:{self.session.id}:{key}",
            method="on_output",
            payload={"text": f"answer-{key}"},
        )
        if key in self.ids:
            assert row.id == self.ids[key]
        else:
            self.ids[key] = row.id
            self.states[key] = "pending"

    @rule()
    def claim(self) -> None:
        """A competing drainer cannot overtake an unacknowledged earlier delivery."""
        rows = self.store.claim_pending_bridge_deliveries(
            self.session.id, "telegram", limit=1
        )
        outstanding = [key for key in self.ids if self.states[key] != "sent"]
        if self.active is not None or not outstanding:
            assert rows == []
            return
        key = outstanding[0]
        assert [row.id for row in rows] == [self.ids[key]]
        self.states[key] = "sending"
        self.active = key

    @precondition(lambda self: self.active is not None)
    @rule(success=st.booleans())
    def finish(self, success: bool) -> None:
        """Acknowledgment is terminal; a failed attempt leaves the same row retryable."""
        key = self.active
        assert key is not None
        if success:
            self.store.mark_bridge_delivery_sent(self.ids[key])
            self.states[key] = "sent"
        else:
            self.store.mark_bridge_delivery_failed(
                self.ids[key], RuntimeError("offline"), retry_delay_s=0
            )
            self.states[key] = "pending"
        self.active = None

    @rule()
    def reload_store(self) -> None:
        """Store reloads preserve pending work, acknowledged rows and active leases."""
        self.store = SessionStore()

    @invariant()
    def durable_rows_match_model(self) -> None:
        """Account for every payload and state independently of the management API."""
        with get_db_session() as db:
            rows = db.exec(
                select(BridgeDelivery).where(
                    BridgeDelivery.session_id == self.session.id
                )
            ).all()
        actual = {row.id: (row.status, json.loads(row.payload)["text"]) for row in rows}
        assert actual == {
            self.ids[key]: (state, f"answer-{key}")
            for key, state in self.states.items()
        }

    def teardown(self) -> None:
        """Release the database even when Hypothesis shrinks a failing sequence."""
        self.isolation.__exit__(None, None, None)


TestBridgeOutbox = BridgeOutboxMachine.TestCase
TestBridgeOutbox.settings = settings(
    max_examples=25,
    stateful_step_count=20,
    deadline=None,
    database=None,
    derandomize=True,
)
