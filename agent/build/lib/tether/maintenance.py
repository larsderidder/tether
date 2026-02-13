"""Background maintenance tasks for pruning and idle timeouts."""

from __future__ import annotations

import asyncio
import time

import structlog

from tether.api.emit import emit_state
from tether.api.runner_events import get_api_runner
from tether.api.state import session_lock, transition
from tether.models import SessionState
from tether.settings import settings
from tether.store import store

logger = structlog.get_logger(__name__)


def _parse_ts(value: str) -> float | None:
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return None


MAINTENANCE_INTERVAL_SECONDS = 60


async def maintenance_loop() -> None:
    """Periodically prune sessions and stop idle runs."""
    retention_days = settings.session_retention_days()
    idle_timeout_s = settings.session_idle_timeout_seconds()
    interval_s = MAINTENANCE_INTERVAL_SECONDS
    while True:
        try:
            removed = store.prune_sessions(retention_days)
            if removed:
                logger.info("Pruned sessions", count=removed)
            if idle_timeout_s > 0:
                now_ts = time.time()
                for session in list(store.list_sessions()):
                    if session.state != SessionState.RUNNING:
                        continue
                    last = _parse_ts(session.last_activity_at)
                    if last is None:
                        continue
                    if now_ts - last > idle_timeout_s:
                        sid = session.id
                        logger.warning("Idle timeout reached; interrupting session", session_id=sid)

                        # Phase 1: transition under lock
                        async with session_lock(sid):
                            session = store.get_session(sid)
                            if not session or session.state != SessionState.RUNNING:
                                continue
                            transition(session, SessionState.INTERRUPTING)
                            await emit_state(session)
                            adapter = session.adapter

                        # Phase 2: stop runner (lock released so callbacks
                        # can acquire it without deadlocking)
                        try:
                            await get_api_runner(adapter).stop(sid)
                        except Exception:
                            logger.exception("Failed to stop idle session", session_id=sid)

                        # Phase 3: finalize under lock
                        async with session_lock(sid):
                            session = store.get_session(sid)
                            if session and session.state == SessionState.INTERRUPTING:
                                transition(session, SessionState.AWAITING_INPUT)
                                await emit_state(session)
        except Exception:
            logger.exception("Maintenance loop failed")
        await asyncio.sleep(interval_s)
