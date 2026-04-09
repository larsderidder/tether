"""Telegram bridge wrapper with compatibility fixes."""

from __future__ import annotations

from typing import Any

import structlog
from agent_tether.telegram.bot import TelegramBridge as _BaseTelegramBridge

logger = structlog.get_logger(__name__)

__all__ = ["TelegramBridge"]


class TelegramBridge(_BaseTelegramBridge):
    """Tether Telegram bridge wrapper."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Compatibility shim for older agent-tether versions.
        if not hasattr(self, "_external_view_by_user"):
            self._external_view_by_user: dict[int, list[dict]] = {}

    def _on_polling_error(self, error: Exception) -> None:
        """Log Telegram polling errors without giant tracebacks."""
        logger.warning(
            "Telegram polling error",
            error_type=error.__class__.__name__,
            error=str(error),
        )

    async def start(self) -> None:
        """Start Telegram bridge with cleaner polling error logging."""
        from telegram.ext import Updater

        original_start_polling = Updater.start_polling

        async def _start_polling_with_error_callback(updater: Any, *args: Any, **kwargs: Any):
            kwargs.setdefault("error_callback", self._on_polling_error)
            return await original_start_polling(updater, *args, **kwargs)

        Updater.start_polling = _start_polling_with_error_callback
        try:
            await super().start()
        finally:
            Updater.start_polling = original_start_polling

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
