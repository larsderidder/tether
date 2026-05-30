"""Tests for bridge send retry helpers."""

from __future__ import annotations

import pytest

from tether.bridges.retry import with_bridge_send_retry


class RetryableError(Exception):
    """Fake retryable platform exception."""

    def __init__(self, status: int, retry_after: float | None = None) -> None:
        super().__init__(f"status {status}")
        self.status = status
        self.retry_after = retry_after


@pytest.mark.anyio
async def test_with_bridge_send_retry_retries_429(monkeypatch) -> None:
    """Transient rate-limit errors are retried."""

    sleeps: list[float] = []
    calls = 0

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def send() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryableError(429, retry_after=0.25)
        return "ok"

    monkeypatch.setattr("tether.bridges.retry.asyncio.sleep", fake_sleep)

    assert await with_bridge_send_retry("test", send) == "ok"
    assert calls == 2
    assert sleeps == [0.25]


@pytest.mark.anyio
async def test_with_bridge_send_retry_does_not_retry_400(monkeypatch) -> None:
    """Permanent platform errors fail immediately."""

    sleeps: list[float] = []
    calls = 0

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def send() -> str:
        nonlocal calls
        calls += 1
        raise RetryableError(400)

    monkeypatch.setattr("tether.bridges.retry.asyncio.sleep", fake_sleep)

    with pytest.raises(RetryableError):
        await with_bridge_send_retry("test", send)
    assert calls == 1
    assert sleeps == []
