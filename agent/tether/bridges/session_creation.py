"""Shared directory history and session configuration notices for bridges."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from agent_tether.base import BridgeCallbacks, BridgeInterface
from agent_tether.text_command_bridge import TextCommandBridge

from tether.adapter_names import adapter_label, normalize_adapter_name
from tether.settings import settings

logger = structlog.get_logger(__name__)


async def recent_directories(
    callbacks: BridgeCallbacks, *, current_directory: str | None = None
) -> list[str]:
    """List existing workspaces by activity, with the current directory first."""
    sessions = list(await callbacks.list_sessions())
    if current_directory:
        sessions.append({"directory": current_directory})
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

        directories = sorted(by_directory, key=lambda path: (-by_directory[path], path))
        if current_directory:
            try:
                current = str(Path(current_directory).expanduser().resolve())
                if current in directories:
                    directories.remove(current)
                    directories.insert(0, current)
            except (OSError, RuntimeError, ValueError):
                pass
        return directories[:50]

    return await asyncio.to_thread(collect)


class SessionCreationMixin(BridgeInterface):
    """Apply Tether's new-session defaults consistently across all bridges."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Give upstream naming and command paths the same canonical default."""
        super().__init__(*args, **kwargs)
        self._config.default_adapter = normalize_adapter_name(
            self._config.default_adapter or settings.adapter()
        )

    async def _parse_new_args(
        self, args: str, *, base_session_id: str | None
    ) -> tuple[str | None, str]:
        """Reuse the parent's directory, never its adapter implicitly."""
        parts = (args or "").split(maxsplit=1)
        adapter = self._config.default_adapter
        directory_raw = (args or "").strip()
        if parts and self._agent_to_adapter(parts[0]):
            adapter = self._agent_to_adapter(parts[0])
            directory_raw = parts[1] if len(parts) > 1 else ""

        base = (
            self._get_session_info(base_session_id)
            if base_session_id and self._get_session_info
            else None
        )
        base_directory = (base or {}).get("directory")
        directory_raw = directory_raw or base_directory or ""
        if not directory_raw:
            raise ValueError(
                "Usage: !new <agent> <directory> (default: Pi; also Claude, Codex, OpenCode)"
            )
        directory = await self._resolve_directory_arg(
            directory_raw, base_directory=base_directory
        )
        return adapter, directory

    # Telegram's upstream bridge does not inherit TextCommandBridge.
    _parse_new_args_extended = TextCommandBridge._parse_new_args_extended

    async def _handle_new_extended(
        self,
        event_ctx: object,
        parsed: dict,
        *,
        platform: str,
        base_session_id: str | None = None,
    ) -> str:
        """Use explicit flags or template settings, not the parent agent/model."""
        parsed = dict(parsed)
        if not parsed.get("adapter") and not parsed.get("template"):
            parsed["adapter"] = self._config.default_adapter
        return await TextCommandBridge._handle_new_extended(
            self, event_ctx, parsed, platform=platform, base_session_id=None
        )

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
