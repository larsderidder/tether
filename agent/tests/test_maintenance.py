"""Tests for maintenance helpers."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from tether.maintenance import _parse_ts


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
