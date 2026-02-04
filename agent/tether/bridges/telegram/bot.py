"""Telegram bot bridge implementation."""

import asyncio
import os
from typing import Any

import structlog

from tether.bridges.base import ApprovalRequest, BridgeInterface
from tether.bridges.telegram.formatting import chunk_message, escape_markdown
from tether.bridges.telegram.state import StateManager
from tether.settings import settings

logger = structlog.get_logger(__name__)


class TelegramBridge(BridgeInterface):
    """Telegram bridge that routes agent events to Telegram forum topics.

    Each session gets its own forum topic. Implements the BridgeInterface
    to handle output, approvals, and status updates.

    Args:
        bot_token: Telegram bot API token.
        forum_group_id: Telegram forum group chat ID.
        state_manager: Optional state manager (created if not provided).
    """

    def __init__(
        self,
        bot_token: str,
        forum_group_id: int,
        state_manager: StateManager | None = None,
    ):
        self._bot_token = bot_token
        self._forum_group_id = forum_group_id
        self._app: Any = None
        self._state = state_manager or StateManager(
            os.path.join(settings.data_dir(), "telegram_state.json")
        )
        self._state.load()

    async def start(self) -> None:
        """Start the Telegram bot."""
        try:
            from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters
        except ImportError:
            logger.error("python-telegram-bot not installed. Install with: pip install python-telegram-bot")
            return

        self._app = Application.builder().token(self._bot_token).build()

        # Add message handler for human input
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.SUPERGROUP,
                self._handle_message,
            )
        )

        # Add callback query handler for approval buttons
        self._app.add_handler(
            CallbackQueryHandler(self._handle_callback_query, pattern=r"^approval:")
        )

        await self._app.initialize()
        await self._app.start()
        logger.info("Telegram bridge initialized and started", forum_group_id=self._forum_group_id)

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        if self._app:
            await self._app.stop()
            await self._app.shutdown()
        logger.info("Telegram bridge stopped")

    async def _handle_message(self, update: Any, context: Any) -> None:
        """Handle incoming messages from Telegram and forward to event log.

        Args:
            update: Telegram update object.
            context: Telegram context object.
        """
        if not update.message or not update.message.text:
            return

        # Get topic ID
        topic_id = update.message.message_thread_id
        if not topic_id:
            return

        # Look up session for this topic
        session_id = self._state.get_session_for_topic(topic_id)
        if not session_id:
            logger.debug(
                "Received message in topic with no session mapping",
                topic_id=topic_id,
            )
            return

        # Import store here to avoid circular import
        from tether.store import store

        # Emit human_input event
        try:
            await store.emit(session_id, {
                "session_id": session_id,
                "ts": store._now(),
                "seq": store.next_seq(session_id),
                "type": "human_input",
                "data": {
                    "text": update.message.text,
                    "username": update.message.from_user.username or "unknown",
                    "user_id": str(update.message.from_user.id),
                    "platform": "telegram",
                },
            })
            logger.info(
                "Forwarded human input from Telegram",
                session_id=session_id,
                topic_id=topic_id,
                username=update.message.from_user.username,
            )
        except Exception:
            logger.exception(
                "Failed to forward human input",
                session_id=session_id,
                topic_id=topic_id,
            )

    async def _handle_callback_query(self, update: Any, context: Any) -> None:
        """Handle approval button clicks in Telegram.

        Args:
            update: Telegram update object.
            context: Telegram context object.
        """
        query = update.callback_query
        if not query:
            return

        # Acknowledge the callback
        await query.answer()

        # Parse callback data: "approval:request_id:option"
        try:
            parts = query.data.split(":", 2)
            if len(parts) != 3 or parts[0] != "approval":
                logger.warning("Invalid callback data format", data=query.data)
                return

            request_id = parts[1]
            option_selected = parts[2]
        except Exception:
            logger.exception("Failed to parse callback data", data=query.data)
            return

        # Get session ID from topic
        topic_id = query.message.message_thread_id
        if not topic_id:
            logger.warning("Callback from message with no topic ID")
            return

        session_id = self._state.get_session_for_topic(topic_id)
        if not session_id:
            logger.warning("No session for topic", topic_id=topic_id)
            await query.edit_message_text(
                text=f"{query.message.text}\n\n❌ Error: Session not found"
            )
            return

        # Submit approval response via REST API
        try:
            import httpx

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"http://localhost:{settings.port()}/api/external/sessions/{session_id}/approvals/{request_id}/respond",
                    json={
                        "option_selected": option_selected,
                        "username": query.from_user.username or "unknown",
                        "user_id": str(query.from_user.id),
                    },
                    timeout=10.0,
                )
                response.raise_for_status()

            # Update message to show the selection
            username = query.from_user.username or "unknown user"
            await query.edit_message_text(
                text=f"{query.message.text}\n\n✅ Responded: **{option_selected}** by @{username}"
            )

            logger.info(
                "Approval response submitted",
                session_id=session_id,
                request_id=request_id,
                option=option_selected,
                username=username,
            )

        except httpx.HTTPStatusError as e:
            error_msg = "already resolved" if e.response.status_code == 404 else str(e)
            logger.warning(
                "Failed to submit approval",
                session_id=session_id,
                request_id=request_id,
                error=error_msg,
            )
            await query.edit_message_text(
                text=f"{query.message.text}\n\n❌ Error: {error_msg}"
            )
        except Exception:
            logger.exception(
                "Failed to handle callback",
                session_id=session_id,
                request_id=request_id,
            )
            await query.edit_message_text(
                text=f"{query.message.text}\n\n❌ Error: Failed to submit response"
            )

    async def on_output(
        self, session_id: str, text: str, metadata: dict | None = None
    ) -> None:
        """Send output text to the session's Telegram topic.

        Args:
            session_id: Internal Tether session ID.
            text: Output text (markdown format).
            metadata: Optional metadata about the output.
        """
        if not self._app:
            logger.warning("Telegram app not initialized")
            return

        topic_id = self._state.get_topic_for_session(session_id)
        if not topic_id:
            logger.warning(
                "No Telegram topic for session",
                session_id=session_id,
            )
            return

        # Split long messages
        chunks = chunk_message(text)
        for chunk in chunks:
            try:
                await self._app.bot.send_message(
                    chat_id=self._forum_group_id,
                    message_thread_id=topic_id,
                    text=chunk,
                )
            except Exception:
                logger.exception(
                    "Failed to send Telegram message",
                    session_id=session_id,
                    topic_id=topic_id,
                )

    async def on_approval_request(
        self, session_id: str, request: ApprovalRequest
    ) -> None:
        """Send an approval request with inline keyboard buttons.

        Args:
            session_id: Internal Tether session ID.
            request: Approval request details.
        """
        if not self._app:
            logger.warning("Telegram app not initialized")
            return

        topic_id = self._state.get_topic_for_session(session_id)
        if not topic_id:
            logger.warning(
                "No Telegram topic for session",
                session_id=session_id,
            )
            return

        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except ImportError:
            logger.error("python-telegram-bot not installed")
            return

        # Create inline keyboard with options
        keyboard = []
        for option in request.options:
            button = InlineKeyboardButton(
                option,
                callback_data=f"approval:{request.request_id}:{option}",
            )
            keyboard.append([button])

        reply_markup = InlineKeyboardMarkup(keyboard)

        text = f"**Approval Required**\n\n{request.title}\n\n{request.description}"

        try:
            await self._app.bot.send_message(
                chat_id=self._forum_group_id,
                message_thread_id=topic_id,
                text=text,
                reply_markup=reply_markup,
            )
        except Exception:
            logger.exception(
                "Failed to send approval request",
                session_id=session_id,
                request_id=request.request_id,
            )

    async def on_status_change(
        self, session_id: str, status: str, metadata: dict | None = None
    ) -> None:
        """Send status change notification to Telegram.

        Args:
            session_id: Internal Tether session ID.
            status: New status (e.g., "thinking", "executing", "done", "error").
            metadata: Optional metadata about the status.
        """
        if not self._app:
            return

        topic_id = self._state.get_topic_for_session(session_id)
        if not topic_id:
            return

        # Map status to emoji
        emoji_map = {
            "thinking": "💭",
            "executing": "⚙️",
            "done": "✅",
            "error": "❌",
        }
        emoji = emoji_map.get(status, "ℹ️")

        text = f"{emoji} Status: {status}"

        try:
            await self._app.bot.send_message(
                chat_id=self._forum_group_id,
                message_thread_id=topic_id,
                text=text,
            )
        except Exception:
            logger.exception(
                "Failed to send status update",
                session_id=session_id,
                status=status,
            )

    async def create_thread(self, session_id: str, session_name: str) -> dict:
        """Create a Telegram forum topic for a session.

        Args:
            session_id: Internal Tether session ID.
            session_name: Display name for the session.

        Returns:
            Dict with thread_id and platform info.
        """
        if not self._app:
            raise RuntimeError("Telegram app not initialized")

        try:
            # Create forum topic
            topic = await self._app.bot.create_forum_topic(
                chat_id=self._forum_group_id,
                name=session_name[:128],  # Telegram limit
                icon_color=7322096,  # Light blue
            )

            topic_id = topic.message_thread_id

            # Store mapping
            self._state.set_topic_for_session(session_id, topic_id, session_name)

            logger.info(
                "Created Telegram topic",
                session_id=session_id,
                topic_id=topic_id,
                name=session_name,
            )

            return {
                "thread_id": str(topic_id),
                "platform": "telegram",
                "topic_id": topic_id,
            }

        except Exception as e:
            logger.exception(
                "Failed to create Telegram topic",
                session_id=session_id,
                name=session_name,
            )
            raise RuntimeError(f"Failed to create Telegram topic: {e}")
