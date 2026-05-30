"""Small retry helpers for bridge API sends."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import structlog

logger = structlog.get_logger(__name__)

T = TypeVar("T")
_DEFAULT_ATTEMPTS = 3
_DEFAULT_MIN_DELAY_S = 1.0
_DEFAULT_MAX_DELAY_S = 10.0


def _status_code(exc: BaseException) -> int | None:
    """Extract an HTTP status code from common bridge exceptions."""

    for attr in ("status", "status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _retry_after_s(exc: BaseException) -> float | None:
    """Extract a retry-after delay from common bridge exceptions."""

    for attr in ("retry_after", "retryAfter"):
        value = getattr(exc, attr, None)
        if isinstance(value, int | float) and value >= 0:
            return float(value)

    headers = getattr(exc, "headers", None)
    if isinstance(headers, dict):
        raw_value = headers.get("retry-after") or headers.get("Retry-After")
        if isinstance(raw_value, str):
            try:
                parsed = float(raw_value.strip())
            except ValueError:
                return None
            return parsed if parsed >= 0 else None
    return None


def _is_retryable_bridge_error(exc: BaseException) -> bool:
    """Return true for transient platform send failures."""

    status = _status_code(exc)
    if status == 429:
        return True
    if status is not None:
        return status >= 500
    return isinstance(exc, (TimeoutError, OSError, asyncio.TimeoutError))


async def with_bridge_send_retry(
    label: str,
    send: Callable[[], Awaitable[T]],
    *,
    attempts: int = _DEFAULT_ATTEMPTS,
    min_delay_s: float = _DEFAULT_MIN_DELAY_S,
    max_delay_s: float = _DEFAULT_MAX_DELAY_S,
) -> T:
    """Run a platform send with bounded retries for transient errors."""

    last_error: BaseException | None = None
    total_attempts = max(1, attempts)
    for attempt in range(1, total_attempts + 1):
        try:
            return await send()
        except BaseException as exc:
            last_error = exc
            if attempt >= total_attempts or not _is_retryable_bridge_error(exc):
                raise
            retry_after = _retry_after_s(exc)
            delay_s = min(max_delay_s, retry_after if retry_after is not None else min_delay_s * attempt)
            logger.warning(
                "Bridge send failed; retrying",
                label=label,
                attempt=attempt,
                attempts=total_attempts,
                delay_s=delay_s,
                status=_status_code(exc),
            )
            await asyncio.sleep(max(0.0, delay_s))

    assert last_error is not None
    raise last_error
