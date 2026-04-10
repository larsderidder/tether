"""Tether-local Telegram bridge wrapper with richer output formatting."""

from __future__ import annotations

import structlog
from agent_tether.telegram.bot import TelegramBridge as UpstreamTelegramBridge

from tether.bridges.rich_output import render_telegram_messages

logger = structlog.get_logger(__name__)


class TelegramBridge(UpstreamTelegramBridge):
    """Render tool calls and tool output with clearer Telegram formatting."""

    async def on_output(
        self, session_id: str, text: str, metadata: dict | None = None
    ) -> None:
        is_final = bool(metadata and metadata.get("final"))
        if is_final:
            self._stop_typing(session_id)
        if not self._app:
            logger.warning("Telegram app not initialized")
            return

        topic_id = self._state.get_topic_for_session(session_id)
        if not topic_id:
            logger.warning("No Telegram topic for session", session_id=session_id)
            return

        messages = render_telegram_messages(text) or [text]
        for message in messages:
            try:
                await self._app.bot.send_message(
                    chat_id=self._forum_group_id,
                    message_thread_id=topic_id,
                    text=message,
                    parse_mode="HTML",
                )
            except Exception:
                try:
                    await self._app.bot.send_message(
                        chat_id=self._forum_group_id,
                        message_thread_id=topic_id,
                        text=text[:4096],
                    )
                except Exception:
                    logger.exception(
                        "Failed to send Telegram message",
                        session_id=session_id,
                        topic_id=topic_id,
                    )
