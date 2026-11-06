"""Small retry helpers for bridge API sends."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, TypeVar

from agent_tether.base import ApprovalRequest, BridgeInterface
import structlog

if TYPE_CHECKING:
    from tether.store import SessionStore

logger = structlog.get_logger(__name__)

T = TypeVar("T")
_DEFAULT_ATTEMPTS = 3
_DEFAULT_MIN_DELAY_S = 1.0
_DEFAULT_MAX_DELAY_S = 10.0
_BRIDGE_CLAIM_RENEW_INTERVAL_S = 10.0


@asynccontextmanager
async def bridge_delivery_lease(
    store: SessionStore, delivery_id: str
) -> AsyncIterator[None]:
    """Keep an outbox claim alive across slow platform I/O."""

    async def renew() -> None:
        while True:
            await asyncio.sleep(_BRIDGE_CLAIM_RENEW_INTERVAL_S)
            try:
                store.extend_bridge_delivery_claim(delivery_id)
            except Exception:
                logger.exception(
                    "Failed to extend bridge claim", delivery_id=delivery_id
                )

    task = asyncio.create_task(renew())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _status_code(exc: BaseException) -> int | None:
    """Extract an HTTP status code from common bridge exceptions."""

    for attr in ("status", "status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def bridge_retry_after_s(exc: BaseException) -> float | None:
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
    retryable_names = {
        "ConnectError",
        "ConnectTimeout",
        "NetworkError",
        "PoolTimeout",
        "ReadError",
        "ReadTimeout",
        "TimedOut",
        "TimeoutException",
        "WriteError",
        "WriteTimeout",
    }
    if exc.__class__.__name__ in retryable_names:
        return True
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
            retry_after = bridge_retry_after_s(exc)
            delay_s = min(
                max_delay_s,
                retry_after if retry_after is not None else min_delay_s * attempt,
            )
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


async def send_bridge_delivery(
    bridge: BridgeInterface,
    session_id: str,
    method: str,
    payload: dict,
) -> None:
    """Send a persisted bridge delivery through the requested bridge method."""
    if method == "on_output":
        text = str(payload.get("text") or "")
        metadata = payload.get("metadata")

        async def _send_output() -> None:
            await bridge.on_output(
                session_id,
                text,
                metadata=metadata if isinstance(metadata, dict) else None,
            )

        await with_bridge_send_retry("bridge.on_output", _send_output)
        return

    if method == "on_approval_request":
        request_data = payload.get("request")
        if not isinstance(request_data, dict):
            raise ValueError("approval delivery payload is missing request data")
        request = ApprovalRequest.model_validate(
            {
                "kind": str(request_data.get("kind") or "permission"),
                "request_id": str(request_data.get("request_id") or ""),
                "title": str(request_data.get("title") or "Permission request"),
                "description": str(request_data.get("description") or ""),
                "options": list(request_data.get("options") or []),
            }
        )

        async def _send_approval() -> None:
            await bridge.on_approval_request(session_id, request)

        await with_bridge_send_retry("bridge.on_approval_request", _send_approval)
        return

    if method == "on_status_change":
        status = str(payload.get("status") or "")
        metadata = payload.get("metadata")

        async def _send_status() -> None:
            await bridge.on_status_change(
                session_id,
                status,
                metadata if isinstance(metadata, dict) else None,
            )

        await with_bridge_send_retry("bridge.on_status_change", _send_status)
        return

    raise ValueError(f"Unknown bridge delivery method: {method}")
