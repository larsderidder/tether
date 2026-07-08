"""Background watcher for platform-bound external sessions."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from collections.abc import Awaitable, Callable

import structlog

from tether.api.schemas import SyncResult
from tether.models import SessionState
from tether.settings import settings
from tether.store import store

logger = structlog.get_logger(__name__)

SyncFunction = Callable[[str], Awaitable[SyncResult]]


class ExternalSessionWatcher:
    """Poll platform-bound external sessions and sync new history."""

    def __init__(
        self,
        *,
        interval_seconds: float | None = None,
        sync_func: SyncFunction | None = None,
    ) -> None:
        """Create a watcher with optional test overrides."""
        self._interval_seconds = interval_seconds
        self._sync_func = sync_func
        self._session_ids: set[str] = set()
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    def register(self, session_id: str) -> None:
        """Register a session for the next watcher pass."""
        self._session_ids.add(session_id)

    def unregister(self, session_id: str) -> None:
        """Remove a session from the watcher set."""
        self._session_ids.discard(session_id)

    async def start(self) -> None:
        """Start the background polling task."""
        if self._task and not self._task.done():
            return
        if not settings.external_sync_watcher_enabled():
            logger.info("External session watcher disabled")
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="external-session-watcher")
        logger.info(
            "External session watcher started", interval_seconds=self._interval()
        )

    async def stop(self) -> None:
        """Stop the background polling task."""
        if self._stop_event:
            self._stop_event.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        self._stop_event = None
        logger.info("External session watcher stopped")

    async def sync_once(self) -> None:
        """Run one sync pass for all currently eligible sessions."""
        for session_id in self._eligible_session_ids():
            await self._sync_one(session_id)

    async def _run(self) -> None:
        """Run the watcher loop until stopped."""
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            await self.sync_once()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval()
                )
            except asyncio.TimeoutError:
                continue

    async def _sync_one(self, session_id: str) -> None:
        """Sync one eligible session and keep the watcher alive on errors."""
        try:
            await self._sync()(session_id)
        except Exception:
            logger.exception(
                "External session watcher sync failed", session_id=session_id
            )

    def _eligible_session_ids(self) -> list[str]:
        """Return registered and discovered sessions eligible for watcher sync."""
        sessions = store.list_sessions()
        discovered = {
            session.id for session in sessions if self._is_watchable_external(session)
        }
        self._session_ids.intersection_update({session.id for session in sessions})
        self._session_ids.update(discovered)
        return sorted(self._session_ids & discovered)

    def _is_watchable_external(self, session: object) -> bool:
        """Return true for bridge-bound sessions attached to external agents."""
        if not getattr(session, "runner_session_id", None):
            return False
        if not getattr(session, "platform", None):
            return False
        if getattr(session, "state", None) in {
            SessionState.RUNNING,
            SessionState.INTERRUPTING,
        }:
            return False
        has_process = store.get_process(getattr(session, "id", "")) is not None
        if has_process and not self._is_idle_external_pi_session(session):
            return False
        if getattr(session, "external_agent_id", None):
            return True
        if getattr(session, "external_agent_type", None):
            return True
        if not getattr(session, "started_at", None):
            return True
        return self._has_imported_history(str(getattr(session, "id", "")))

    def _is_idle_external_pi_session(self, session: object) -> bool:
        """Return true for attached pi sessions that may receive idle updates."""
        if not getattr(session, "external_agent_id", None):
            return False
        external_type = str(getattr(session, "external_agent_type", "") or "").lower()
        adapter = str(getattr(session, "adapter", "") or "").lower()
        runner_type = str(getattr(session, "runner_type", "") or "").lower()
        return external_type == "pi" or adapter == "pi_rpc" or runner_type == "pi"

    def _has_imported_history(self, session_id: str) -> bool:
        """Return true when a session log contains imported external history."""
        if not session_id:
            return False
        return any(
            (event.get("data") or {}).get("is_history")
            for event in store.read_event_log(session_id, since_seq=0)
        )

    def _interval(self) -> float:
        """Return the configured polling interval."""
        if self._interval_seconds is not None:
            return self._interval_seconds
        return settings.external_sync_interval_seconds()

    def _sync(self) -> SyncFunction:
        """Return the sync function, importing lazily to avoid cycles."""
        if self._sync_func is not None:
            return self._sync_func

        async def _default(session_id: str) -> SyncResult:
            """Sync one session through the shared external sync service."""
            from tether.external_sync import sync_external_session_delta

            return await sync_external_session_delta(
                session_id,
                force=False,
                source="watcher",
                initial_lookback_seconds=settings.external_sync_initial_lookback_seconds(),
            )

        return _default


external_session_watcher = ExternalSessionWatcher()
