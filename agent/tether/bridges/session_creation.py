"""Shared directory history and session configuration notices for bridges."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from agent_tether.base import BridgeCallbacks, BridgeInterface

from tether.adapter_names import adapter_label
from tether.settings import settings

logger = structlog.get_logger(__name__)


async def recent_directories(callbacks: BridgeCallbacks) -> list[str]:
    """List existing directories from Tether and external sessions, newest first."""
    sessions = list(await callbacks.list_sessions())
    try:
        external = await asyncio.wait_for(
            callbacks.list_external_sessions(limit=200), timeout=10
        )
        sessions.extend(external)
    except Exception:
        logger.exception("Could not load external directory history")

    def collect() -> list[str]:
        """Resolve paths off the event loop and collapse duplicate workspaces."""
        by_directory: dict[str, float] = {}
        for session in sessions:
            raw = session.get("directory")
            if not raw:
                continue
            try:
                path = Path(raw).expanduser().resolve()
                if not path.is_dir():
                    continue
            except (OSError, RuntimeError, ValueError):
                continue

            timestamp = session.get("last_activity_at") or session.get("last_activity")
            timestamp = timestamp or session.get("created_at") or ""
            try:
                date = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                activity = date.timestamp()
            except ValueError:
                activity = 0.0
            directory = str(path)
            by_directory[directory] = max(by_directory.get(directory, 0.0), activity)

        return sorted(by_directory, key=lambda path: (-by_directory[path], path))[:50]

    return await asyncio.to_thread(collect)


class SessionCreationMixin(BridgeInterface):
    """Announce the configuration returned by session creation in its new thread."""

    async def _create_session_via_api(self, **kwargs: Any) -> dict:
        """Create once; a failed notice must not turn success into a retry."""
        session = await super()._create_session_via_api(**kwargs)
        adapter = session.get("adapter") or kwargs.get("adapter") or settings.adapter()
        model = session.get("model") or settings.adapter_default_model(adapter)
        label = adapter_label(adapter) or adapter
        notice = (
            f"Agent: {label}\n" f"Model: {model or 'agent default (not reported yet)'}"
        )
        try:
            await self.on_output(session["id"], notice)
        except Exception:
            logger.exception(
                "Could not announce session configuration", session_id=session.get("id")
            )
        return session
