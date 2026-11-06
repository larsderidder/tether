"""Background maintenance tasks for pruning and lifecycle recovery."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from datetime import datetime, timezone

import structlog

from tether.api.emit import emit_error, emit_state
from tether.api.runner_events import get_api_runner, get_runner_registry
from tether.api.state import session_lock, transition
from tether.bridges.glue import bridge_manager, get_sessions_for_restore
from tether.discovery.running import (
    is_claude_session_running,
    is_codex_session_running,
    is_pi_session_running,
)
from tether.models import Session, SessionState
from tether.settings import settings
from tether.store import store

logger = structlog.get_logger(__name__)

MAINTENANCE_INTERVAL_SECONDS = 60
INTERRUPTING_RECOVERY_SECONDS = 300
RUNNER_DISPOSE_TIMEOUT_SECONDS = 15.0
_interrupt_first_seen_at: dict[str, float] = {}


def _parse_ts(value: str) -> float | None:
    """Parse Tether UTC timestamps as epoch seconds."""
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


async def telegram_topic_cleanup_loop() -> None:
    """Close orphaned topics independently of pruning and idle timeouts."""
    while True:
        try:
            bridge = bridge_manager.get_bridge("telegram")
            close_topics = getattr(bridge, "close_orphaned_topics", None)
            if close_topics is not None:
                retired = await close_topics(get_sessions_for_restore)
                if retired:
                    logger.info("Cleaned up orphaned Telegram topics", count=retired)
        except Exception:
            logger.exception("Telegram topic cleanup failed; will retry")
        await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)


async def reconcile_persisted_lifecycle_states() -> None:
    """Reconcile persisted active states after a Tether process restart."""
    for session in list(store.list_sessions()):
        if session.state not in {SessionState.RUNNING, SessionState.INTERRUPTING}:
            continue
        await _reconcile_persisted_session(session.id)


async def _reconcile_persisted_session(session_id: str) -> None:
    """Reconcile one persisted active session without assuming runner ownership."""
    async with session_lock(session_id):
        session = store.get_session(session_id)
        if not session or session.state not in {
            SessionState.RUNNING,
            SessionState.INTERRUPTING,
        }:
            return
        if session.state == SessionState.INTERRUPTING:
            adapter = session.adapter
            external = _is_attached_external_session(session)
            has_process = _has_live_cached_process(session_id)
        elif _has_live_cached_process(session_id):
            return
        elif _is_attached_external_session(session):
            if _is_external_session_running(session):
                logger.info(
                    "External session process still appears live after restart",
                    session_id=session_id,
                )
                return
            transition(session, SessionState.AWAITING_INPUT)
            await emit_state(session)
            logger.info(
                "External session process no longer appears live after restart",
                session_id=session_id,
            )
            return
        else:
            adapter = session.adapter
            external = False
            has_process = False

    current = store.get_session(session_id)
    if current and current.state == SessionState.INTERRUPTING:
        await _finalize_interrupting_session(
            session_id,
            adapter=adapter,
            external=external,
            has_process=has_process,
            reason="Recovered persisted interrupting session",
        )
        return

    recovered = await _recover_owned_runner_session(session_id, adapter)
    if recovered:
        logger.info(
            "Recovered runner-owned active session",
            session_id=session_id,
            adapter=adapter,
        )
        return

    async with session_lock(session_id):
        session = store.get_session(session_id)
        if not session or session.state != SessionState.RUNNING:
            return
        transition(session, SessionState.ERROR, ended_at=True, exit_code=255)
        await emit_state(session)
        await emit_error(
            session,
            "RUNNER_RECOVERY_FAILED",
            "Tether restarted while this session was running, but no functioning runner could be recovered.",
        )
        logger.warning(
            "Marked unrecoverable running session as error",
            session_id=session_id,
            adapter=adapter,
        )


async def maintenance_loop() -> None:
    """Periodically prune sessions, stop idle runs, and recover stuck states."""
    while True:
        try:
            await maintenance_pass()
        except Exception:
            logger.exception("Maintenance loop failed")
        await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)


async def maintenance_pass() -> None:
    """Run one maintenance pass."""
    retention_days = settings.session_retention_days()
    idle_timeout_s = settings.session_idle_timeout_seconds()

    removed = store.prune_sessions(retention_days)
    if removed:
        logger.info("Pruned sessions", count=removed)

    from tether.workspace import prune_stale_repos

    repos_removed = prune_stale_repos()
    if repos_removed:
        logger.info("Pruned stale shared repo clones", count=repos_removed)

    now_ts = time.time()
    await _recover_stale_interrupting_sessions(now_ts, idle_timeout_s)
    await _stop_idle_running_sessions(now_ts, idle_timeout_s)


async def _recover_stale_interrupting_sessions(
    now_ts: float, idle_timeout_s: int
) -> None:
    """Move stale INTERRUPTING sessions only after owned cleanup succeeds."""
    recovery_s = idle_timeout_s if idle_timeout_s > 0 else INTERRUPTING_RECOVERY_SECONDS
    recovery_s = min(recovery_s, INTERRUPTING_RECOVERY_SECONDS)
    active_interrupts: set[str] = set()

    for session in list(store.list_sessions()):
        if session.state != SessionState.INTERRUPTING:
            continue
        active_interrupts.add(session.id)
        interrupt_started_at = _interrupt_started_at(session, now_ts)
        if now_ts - interrupt_started_at < recovery_s:
            continue
        async with session_lock(session.id):
            current = store.get_session(session.id)
            if not current or current.state != SessionState.INTERRUPTING:
                _interrupt_first_seen_at.pop(session.id, None)
                continue
            adapter = current.adapter
            external = _is_attached_external_session(current)
            has_process = _has_live_cached_process(current.id)

        await _finalize_interrupting_session(
            session.id,
            adapter=adapter,
            external=external,
            has_process=has_process,
            reason="Recovered stale interrupting session",
        )

    for session_id in list(_interrupt_first_seen_at):
        if session_id not in active_interrupts:
            _interrupt_first_seen_at.pop(session_id, None)


async def _stop_idle_running_sessions(now_ts: float, idle_timeout_s: int) -> None:
    """Interrupt local runner-owned sessions that exceeded the idle timeout."""
    if idle_timeout_s <= 0:
        return

    for session in list(store.list_sessions()):
        if session.state != SessionState.RUNNING:
            continue
        if _is_attached_external_session(session):
            await _reconcile_external_running_session(session)
            continue
        last = _parse_ts(session.last_activity_at)
        if last is None or now_ts - last <= idle_timeout_s:
            continue
        await _stop_idle_session(session.id)


async def _reconcile_external_running_session(session: Session) -> None:
    """Move attached external sessions to idle once process liveness checks say inactive."""
    if _is_external_session_running(session):
        return
    async with session_lock(session.id):
        current = store.get_session(session.id)
        if not current or current.state != SessionState.RUNNING:
            return
        if not _is_attached_external_session(current) or _is_external_session_running(
            current
        ):
            return
        transition(current, SessionState.AWAITING_INPUT)
        await emit_state(current)
        logger.info(
            "External session process liveness became idle", session_id=session.id
        )


async def _stop_idle_session(session_id: str) -> None:
    """Stop one idle local runner session using the cached runner."""
    logger.warning("Idle timeout reached; interrupting session", session_id=session_id)

    async with session_lock(session_id):
        session = store.get_session(session_id)
        if not session or session.state != SessionState.RUNNING:
            return
        transition(session, SessionState.INTERRUPTING)
        await emit_state(session)
        adapter = session.adapter

    stop_error: Exception | None = None
    try:
        await asyncio.wait_for(
            get_api_runner(adapter).stop(session_id),
            timeout=RUNNER_DISPOSE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        stop_error = exc
        logger.exception("Failed to stop idle session", session_id=session_id)

    async with session_lock(session_id):
        session = store.get_session(session_id)
        if not session or session.state != SessionState.INTERRUPTING:
            return
        if stop_error:
            transition(session, SessionState.ERROR, ended_at=True, exit_code=255)
            await emit_state(session)
            await emit_error(
                session, "RUNNER_STOP_FAILED", "Failed to stop idle runner session."
            )
            return
        transition(session, SessionState.AWAITING_INPUT)
        await emit_state(session)


async def _finalize_interrupting_session(
    session_id: str,
    *,
    adapter: str | None,
    external: bool,
    has_process: bool,
    reason: str,
) -> None:
    """Resolve an INTERRUPTING session without holding its state lock during cleanup."""
    cleanup_error: str | None = None
    if external:
        session = store.get_session(session_id)
        if session and _is_external_session_running(session):
            cleanup_error = "Attached external process still appears live; not marking it input-ready."
    else:
        try:
            runner = get_runner_registry().get_runner(adapter)
            dispose_session = getattr(runner, "dispose_session", None)
            if callable(dispose_session):
                disposed = await asyncio.wait_for(
                    dispose_session(session_id),
                    timeout=RUNNER_DISPOSE_TIMEOUT_SECONDS,
                )
                if disposed is False and has_process:
                    cleanup_error = (
                        "Runner reported no disposable resources for a live process."
                    )
            elif has_process:
                await asyncio.wait_for(
                    runner.stop(session_id),
                    timeout=RUNNER_DISPOSE_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError:
            cleanup_error = "Timed out disposing runner-owned process."
            logger.exception(
                "Timed out disposing interrupting session", session_id=session_id
            )
        except asyncio.CancelledError:
            cleanup_error = "Disposing runner-owned process was cancelled."
            logger.warning(
                "Cancelled disposing interrupting session", session_id=session_id
            )
        except Exception as exc:
            cleanup_error = f"Failed to dispose runner-owned process: {exc}"
            logger.exception(
                "Failed to dispose interrupting session", session_id=session_id
            )

    async with session_lock(session_id):
        session = store.get_session(session_id)
        if not session or session.state != SessionState.INTERRUPTING:
            _interrupt_first_seen_at.pop(session_id, None)
            return
        if cleanup_error:
            transition(session, SessionState.ERROR, ended_at=True, exit_code=255)
            await emit_state(session)
            await emit_error(session, "RUNNER_STOP_FAILED", cleanup_error)
            logger.warning(
                "Failed to recover interrupting session", session_id=session_id
            )
            return
        transition(session, SessionState.AWAITING_INPUT)
        await emit_state(session)
        _interrupt_first_seen_at.pop(session_id, None)
        logger.warning(reason, session_id=session_id)


async def _recover_owned_runner_session(session_id: str, adapter: str | None) -> bool:
    """Ask the adapter to recover a Tether-owned running session, if supported."""
    try:
        runner = get_runner_registry().get_runner(adapter)
    except Exception:
        logger.exception(
            "Failed to create runner during startup recovery",
            session_id=session_id,
            adapter=adapter,
        )
        return False

    recover = getattr(runner, "recover_session", None)
    if not callable(recover):
        logger.info(
            "Runner has no startup recovery hook",
            session_id=session_id,
            adapter=adapter,
        )
        return False

    try:
        return bool(await recover(session_id))
    except Exception:
        logger.exception(
            "Runner recovery hook failed", session_id=session_id, adapter=adapter
        )
        return False


def _interrupt_started_at(session: Session, now_ts: float) -> float:
    """Return the first known interrupt timestamp independent of heartbeats."""
    logged_at = _interrupt_started_at_from_events(session.id)
    if logged_at is not None:
        _interrupt_first_seen_at.pop(session.id, None)
        return logged_at
    return _interrupt_first_seen_at.setdefault(session.id, now_ts)


def _interrupt_started_at_from_events(session_id: str) -> float | None:
    """Find the latest persisted transition into INTERRUPTING for one session."""
    for event in reversed(store.read_event_log(session_id)):
        if event.get("type") != "session_state":
            continue
        data = event.get("data") or {}
        if data.get("state") != SessionState.INTERRUPTING.value:
            continue
        return _parse_ts(str(event.get("ts") or ""))
    return None


def _has_live_cached_process(session_id: str) -> bool:
    """Return true when this Tether process still has a live process handle."""
    proc = store.get_process(session_id)
    return proc is not None and getattr(proc, "returncode", None) is None


def _is_attached_external_session(session: Session) -> bool:
    """Return true for sessions attached to an agent not owned by this Tether process."""
    if not session.runner_session_id:
        return False
    return bool(session.external_agent_id or session.external_agent_type)


def _is_external_session_running(session: Session) -> bool:
    """
    Return true when discovery reports an attached external process as live.

    This is process liveness only. It does not prove an agent turn is active.
    """
    runner_session_id = session.runner_session_id
    if not runner_session_id:
        return False

    external_type = (session.external_agent_type or "").lower()
    with suppress(Exception):
        if external_type == "pi":
            return is_pi_session_running(runner_session_id)
        if external_type == "codex":
            return is_codex_session_running(runner_session_id)
        if external_type == "claude_code":
            return is_claude_session_running(runner_session_id)

    return False
