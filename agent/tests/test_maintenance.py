"""Tests for maintenance helpers."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from tether import maintenance
from tether.maintenance import _parse_ts
from tether.models import SessionState
from tether.settings import settings
from tether.store import SessionStore


def test_parse_ts_treats_z_suffix_as_utc(monkeypatch) -> None:
    """_parse_ts should parse ISO Z timestamps in UTC, independent of local TZ."""
    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Etc/GMT-2")
    if hasattr(time, "tzset"):
        time.tzset()

    try:
        actual = _parse_ts("2026-01-01T00:00:00Z")
        expected = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        assert actual == expected
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        if hasattr(time, "tzset"):
            time.tzset()


def test_parse_ts_returns_none_for_invalid_value() -> None:
    """Invalid timestamps should not raise inside maintenance loop."""
    assert _parse_ts("not-a-timestamp") is None


@pytest.mark.anyio
async def test_topic_cleanup_loop_retries_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cleanup pass does not disable subsequent cleanup passes."""
    bridge = MagicMock()
    bridge.close_orphaned_topics = AsyncMock(side_effect=[RuntimeError("offline"), 1])
    monkeypatch.setattr(maintenance.bridge_manager, "get_bridge", lambda _: bridge)
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr(maintenance.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await maintenance.telegram_topic_cleanup_loop()

    assert bridge.close_orphaned_topics.await_count == 2
    bridge.close_orphaned_topics.assert_awaited_with(
        maintenance.get_sessions_for_restore
    )


@pytest.mark.anyio
async def test_topic_cleanup_loop_without_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Servers without Telegram leave cleanup idle."""
    monkeypatch.setattr(maintenance.bridge_manager, "get_bridge", lambda _: None)
    monkeypatch.setattr(
        maintenance.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError())
    )

    with pytest.raises(asyncio.CancelledError):
        await maintenance.telegram_topic_cleanup_loop()


@pytest.mark.parametrize(
    ("age_days", "state", "retained"),
    [
        (14, SessionState.AWAITING_INPUT, True),
        (31, SessionState.AWAITING_INPUT, False),
        (31, SessionState.RUNNING, True),
        (31, SessionState.INTERRUPTING, True),
    ],
)
def test_default_retention_keeps_sessions_for_thirty_days(
    fresh_store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
    age_days: int,
    state: SessionState,
    retained: bool,
) -> None:
    """Inactive sessions expire after 30 days; active runs never expire."""
    monkeypatch.delenv("TETHER_AGENT_SESSION_RETENTION_DAYS", raising=False)
    session = fresh_store.create_session("repo_test", "main")
    session.state = state
    session.last_activity_at = (
        datetime.now(timezone.utc) - timedelta(days=age_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_store.update_session(session)

    fresh_store.prune_sessions(settings.session_retention_days())

    assert (fresh_store.get_session(session.id) is not None) == retained
