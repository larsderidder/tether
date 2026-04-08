"""Telegram bridge wrapper with compatibility fixes."""

from __future__ import annotations

from typing import Any

from agent_tether.telegram.bot import TelegramBridge as _BaseTelegramBridge

__all__ = ["TelegramBridge"]


class TelegramBridge(_BaseTelegramBridge):
    """Tether Telegram bridge wrapper."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Compatibility shim for older agent-tether versions.
        if not hasattr(self, "_external_view_by_user"):
            self._external_view_by_user: dict[int, list[dict]] = {}

    async def _cmd_list(self, update: Any, context: Any) -> None:
        await super()._cmd_list(update, context)
        user_id = getattr(getattr(update, "effective_user", None), "id", None)
        view = getattr(self, "_external_view", None)
        if isinstance(user_id, int) and isinstance(view, list):
            self._external_view_by_user[user_id] = list(view)

    async def _cmd_attach(self, update: Any, context: Any) -> None:
        user_id = getattr(getattr(update, "effective_user", None), "id", None)
        user_view = (
            self._external_view_by_user.get(user_id)
            if isinstance(user_id, int)
            else None
        )

        if isinstance(user_view, list) and hasattr(self, "_external_view"):
            original_view = list(getattr(self, "_external_view", []))
            try:
                self._external_view = list(user_view)
                await super()._cmd_attach(update, context)
            finally:
                self._external_view = original_view
            return

        await super()._cmd_attach(update, context)
