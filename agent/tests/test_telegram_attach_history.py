"""Pi attachment history stays within both configured replay limits."""

from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent_sessions import RunnerType, SessionDetail, SessionMessage

from tether.bridges.glue import bridge_manager
from tether.external_sync import recent_attach_history
from tether.models import SessionState
from tether.store import SessionStore


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


class FixedDatetime(datetime):
    """Keep history cutoffs independent of wall-clock time."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        """Return the fixed instant with the requested timezone awareness."""
        return NOW if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def fixed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze only the clock used for history selection."""
    monkeypatch.setattr("tether.external_sync.datetime", FixedDatetime)


def conversation(turns: int, seconds_apart: int) -> list[SessionMessage]:
    """Each turn has a user prompt, assistant progress, and a final reply."""
    messages = []
    for index in range(turns):
        timestamp = (
            NOW - timedelta(seconds=seconds_apart * (turns - index - 1))
        ).isoformat()
        messages.extend(
            [
                SessionMessage(
                    role="user", content=f"question-{index}", timestamp=timestamp
                ),
                SessionMessage(
                    role="assistant", content=f"progress-{index}", timestamp=timestamp
                ),
                SessionMessage(
                    role="assistant", content=f"answer-{index}", timestamp=timestamp
                ),
            ]
        )
    return messages


@pytest.mark.parametrize(
    ("turns", "seconds_apart", "start"),
    [(40, 120, 72), (40, 20, 70), (40, 60, 70), (4, 1200, 6), (80, 10, 190)],
)
def test_history_uses_shorter_window(
    fixed_time, turns: int, seconds_apart: int, start: int
) -> None:
    """Replay at most 50 messages, all from the last 30 minutes."""
    messages = conversation(turns, seconds_apart)
    assert recent_attach_history(messages) == (messages[start:], start)


def test_history_does_not_include_prompt_before_cutoff(fixed_time) -> None:
    """Keeping a complete turn must not extend the age limit."""
    messages = conversation(1, 60)
    messages[0].timestamp = (NOW - timedelta(minutes=31)).isoformat()
    messages[1].timestamp = (NOW - timedelta(minutes=30)).isoformat()

    assert recent_attach_history(messages) == (messages[1:], 1)


@pytest.mark.parametrize(
    "timestamp", [None, "invalid", (NOW - timedelta(minutes=31)).isoformat()]
)
def test_history_without_recent_timestamps_is_empty(fixed_time, timestamp) -> None:
    """Old or unknown timestamps cannot bypass the age limit."""
    messages = conversation(40, 120)
    for message in messages:
        message.timestamp = timestamp
    assert recent_attach_history(messages) == ([], len(messages))
    assert recent_attach_history([]) == ([], 0)


@pytest.mark.parametrize(
    ("max_messages", "minutes", "start"),
    [(7, 30, 113), (50, 1, 108), (0, 30, 120), (50, 0, 120)],
)
def test_history_limits_are_configurable(
    fixed_time,
    monkeypatch: pytest.MonkeyPatch,
    max_messages: int,
    minutes: int,
    start: int,
) -> None:
    """Both configured limits apply, and either can disable attachment replay."""
    monkeypatch.setenv("TETHER_TELEGRAM_ATTACH_HISTORY_MAX_MESSAGES", str(max_messages))
    monkeypatch.setenv("TETHER_TELEGRAM_ATTACH_HISTORY_MINUTES", str(minutes))
    messages = conversation(40, 20)

    assert recent_attach_history(messages) == (messages[start:], start)


@pytest.mark.anyio
@pytest.mark.parametrize("existing", [False, True])
async def test_pi_telegram_attach_replays_bounded_history(
    api_client,
    fresh_store: SessionStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixed_time,
    existing: bool,
) -> None:
    """First attachment and rebinding send no more than the selected history."""
    messages = conversation(40, 20)
    detail = SessionDetail(
        id="pi-external",
        runner_type=RunnerType.PI,
        directory=str(tmp_path),
        first_prompt="question-0",
        last_prompt="question-39",
        last_activity=NOW.isoformat(),
        message_count=len(messages),
        is_running=False,
        messages=messages,
    )
    monkeypatch.setattr(
        "tether.api.external_sessions.get_external_session_detail",
        lambda session_id, runner_type, limit: detail.model_copy(
            update={"messages": messages[-limit:]}
        ),
    )
    monkeypatch.setattr(
        "tether.bridges.glue.create_or_reuse_thread",
        AsyncMock(return_value={"thread_id": "456"}),
    )
    monkeypatch.setattr("tether.bridges.glue.bridge_subscriber.subscribe", MagicMock())
    bridge = AsyncMock()
    bridge_manager.register_bridge("telegram", bridge)
    if existing:
        session = fresh_store.create_session(str(tmp_path), None)
        session.directory = str(tmp_path)
        session.runner_type = "pi"
        session.adapter = "pi_rpc"
        session.state = SessionState.AWAITING_INPUT
        fresh_store.update_session(session)
        fresh_store.set_runner_session_id(session.id, "pi-external")

    response = await api_client.post(
        "/api/sessions/attach",
        json={
            "external_id": "pi-external",
            "runner_type": "pi",
            "directory": str(tmp_path),
            "platform": "telegram",
        },
    )

    assert response.status_code == 201
    session_id = response.json()["id"]
    delivered = "\n".join(call.args[1] for call in bridge.on_output.await_args_list)
    assert "progress-23" in delivered
    assert "answer-39" in delivered
    assert "question-23" not in delivered
    assert "answer-22" not in delivered
    assert bridge.on_output.await_count <= 50
    assert fresh_store.get_synced_message_count(session_id) == len(messages)
