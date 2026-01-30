"""Telegram bot handlers and bridge logic."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

if TYPE_CHECKING:
    from .config import Config
    from .sse_client import AgentClient

logger = logging.getLogger(__name__)


class TelegramBridge:
    """Telegram bot that bridges to Tether agent.

    Subscribes to agent SSE events and sends Telegram notifications when
    input is required. Routes user replies back to the agent as session input.

    Args:
        config: Bridge configuration with bot token and chat ID.
        agent: HTTP client for agent communication.
    """

    def __init__(self, config: "Config", agent: "AgentClient"):
        self._config = config
        self._agent = agent
        self._app: Application | None = None
        self._active_session_id: str | None = None
        self._sessions: dict[str, dict] = {}
        self._external_sessions: list[dict] = []
        self._subscriptions: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        """Start the Telegram bot."""
        self._app = Application.builder().token(self._config.telegram_bot_token).build()

        self._app.add_handler(CommandHandler("status", self._handle_status))
        self._app.add_handler(CommandHandler("sessions", self._handle_status))
        self._app.add_handler(CommandHandler("stop", self._handle_stop))
        self._app.add_handler(CommandHandler("switch", self._handle_switch))
        self._app.add_handler(CommandHandler("external", self._handle_external))
        self._app.add_handler(CommandHandler("attach", self._handle_attach))
        self._app.add_handler(CommandHandler("help", self._handle_help))
        self._app.add_handler(CommandHandler("start", self._handle_help))
        self._app.add_handler(
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND
                & filters.Chat(self._config.telegram_chat_id),
                self._handle_message,
            )
        )

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

        await self._refresh_sessions()
        logger.info("Telegram bot started")

    async def stop(self) -> None:
        """Stop the bot and cancel subscriptions."""
        for task in self._subscriptions.values():
            if not task.done():
                task.cancel()
        self._subscriptions.clear()

        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        logger.info("Telegram bot stopped")

    async def _refresh_sessions(self) -> None:
        """Refresh session list and subscribe to active sessions."""
        try:
            sessions = await self._agent.list_sessions()
            for session in sessions:
                session_id = session["id"]
                self._sessions[session_id] = session

                if session.get("state") in ("RUNNING", "AWAITING_INPUT"):
                    self._ensure_subscription(session_id)
        except Exception:
            logger.exception("Failed to refresh sessions")

    def _ensure_subscription(self, session_id: str) -> None:
        """Ensure we're subscribed to a session's events."""
        existing = self._subscriptions.get(session_id)
        if existing and not existing.done():
            return

        task = asyncio.create_task(self._subscribe_loop(session_id))
        self._subscriptions[session_id] = task

    async def _subscribe_loop(self, session_id: str) -> None:
        """Subscribe to SSE events for a session."""
        logger.info("Subscribing to session %s", session_id)
        try:
            async for event in self._agent.subscribe(session_id):
                await self._handle_sse_event(session_id, event)
        except asyncio.CancelledError:
            logger.info("Subscription cancelled for %s", session_id)
        except Exception:
            logger.exception("SSE error for %s", session_id)

    async def _handle_sse_event(self, session_id: str, event: dict) -> None:
        """Handle an SSE event from the agent."""
        event_type = event.get("type")
        data = event.get("data", {})

        if event_type == "input_required":
            await self._notify_input_required(session_id, data)
            self._active_session_id = session_id

        elif event_type == "session_state":
            state = data.get("state")
            session = self._sessions.get(session_id)
            if session:
                session["state"] = state

            if state == "ERROR":
                session_name = session.get("name") if session else session_id
                await self._send_message(f"❌ Session error: {session_name}")

    async def _notify_input_required(self, session_id: str, data: dict) -> None:
        """Send Telegram notification that input is required."""
        session = self._sessions.get(session_id)
        session_name = (
            data.get("session_name")
            or (session.get("name") if session else None)
            or session_id[:12]
        )
        last_output = data.get("last_output") or ""

        lines = ["📝 *Input Required*", f"Session: {self._escape_md(session_name)}"]
        if last_output:
            truncated = (
                last_output[:400] + "..." if len(last_output) > 400 else last_output
            )
            lines.append(f"\n```\n{truncated}\n```")
        lines.append("\n_Reply to send input to the agent_")

        await self._send_message("\n".join(lines), parse_mode="Markdown")

    async def _send_message(self, text: str, **kwargs) -> None:
        """Send a message to the configured chat."""
        if self._app:
            await self._app.bot.send_message(
                chat_id=self._config.telegram_chat_id,
                text=text,
                **kwargs,
            )

    async def _handle_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /help and /start commands."""
        help_text = """*Tether Telegram Bridge*

Commands:
/status - List all sessions
/external - List external sessions (Claude Code, Codex)
/attach <id> - Attach to an external session
/stop [id] - Interrupt active or specified session
/switch <id> - Switch active session
/help - Show this help

Send any text message to forward as input to the active session.
"""
        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def _handle_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /status command."""
        await self._refresh_sessions()

        if not self._sessions:
            await update.message.reply_text("No sessions found.")
            return

        lines = ["*Sessions:*"]
        for i, (sid, session) in enumerate(self._sessions.items(), 1):
            state = session.get("state", "UNKNOWN")
            name = session.get("name") or sid[:12]
            active = " ✓" if sid == self._active_session_id else ""
            state_emoji = self._state_emoji(state)
            lines.append(f"{i}. {state_emoji} {self._escape_md(name)}{active}")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _handle_stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /stop command (interrupts the current turn)."""
        args = context.args

        if args:
            session_id = self._resolve_session_id(args[0])
        else:
            session_id = self._active_session_id

        if not session_id:
            await update.message.reply_text(
                "No session specified and no active session."
            )
            return

        try:
            await self._agent.interrupt_session(session_id)
            session = self._sessions.get(session_id)
            name = session.get("name") if session else session_id[:12]
            await update.message.reply_text(f"⏹️ Interrupted: {name}")
        except Exception as e:
            await update.message.reply_text(f"Failed to interrupt session: {e}")

    async def _handle_switch(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /switch command."""
        args = context.args

        if not args:
            await update.message.reply_text("Usage: /switch <session\\_id or number>")
            return

        session_id = self._resolve_session_id(args[0])
        if not session_id:
            await update.message.reply_text(f"Session not found: {args[0]}")
            return

        self._active_session_id = session_id
        self._ensure_subscription(session_id)
        session = self._sessions.get(session_id)
        name = session.get("name") if session else session_id[:12]
        await update.message.reply_text(f"Switched to: {name}")

    async def _handle_external(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /external command - list external sessions."""
        try:
            self._external_sessions = await self._agent.list_external_sessions(limit=10)
        except Exception as e:
            await update.message.reply_text(f"Failed to list external sessions: {e}")
            return

        if not self._external_sessions:
            await update.message.reply_text(
                "No external sessions found.\n\n"
                "Start a Claude Code or Codex session first, then use /external to see it."
            )
            return

        lines = ["*External Sessions:*"]
        for i, session in enumerate(self._external_sessions, 1):
            runner = session.get("runner_type", "unknown")
            directory = session.get("directory", "")
            dir_short = directory.split("/")[-1] if directory else "unknown"
            is_running = "🟢" if session.get("is_running") else "⚪"
            prompt = session.get("first_prompt") or ""
            prompt_short = (prompt[:30] + "...") if len(prompt) > 30 else prompt
            lines.append(
                f"{i}. {is_running} *{self._escape_md(runner)}* in `{self._escape_md(dir_short)}`"
            )
            if prompt_short:
                lines.append(f"   _{self._escape_md(prompt_short)}_")

        lines.append("\nUse /attach <number> to attach to a session.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _handle_attach(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /attach command - attach to an external session."""
        args = context.args

        if not args:
            await update.message.reply_text(
                "Usage: /attach <number>\n\nRun /external first to see available sessions."
            )
            return

        # Resolve the external session
        try:
            index = int(args[0]) - 1
            if not self._external_sessions:
                await update.message.reply_text(
                    "No external sessions cached. Run /external first."
                )
                return
            if index < 0 or index >= len(self._external_sessions):
                await update.message.reply_text(
                    f"Invalid session number. Use 1-{len(self._external_sessions)}."
                )
                return
            external = self._external_sessions[index]
        except ValueError:
            await update.message.reply_text("Please provide a session number.")
            return

        # Attach to the session
        try:
            external_id = external.get("id")
            runner_type = external.get("runner_type")
            directory = external.get("directory")

            result = await self._agent.attach_to_external_session(
                external_id=external_id,
                runner_type=runner_type,
                directory=directory,
            )

            session_id = result.get("id")
            if session_id:
                self._sessions[session_id] = result
                self._active_session_id = session_id
                self._ensure_subscription(session_id)

            name = result.get("name") or session_id[:12] if session_id else "session"
            await update.message.reply_text(
                f"✅ Attached to {runner_type} session\n"
                f"Directory: `{self._escape_md(directory)}`\n"
                f"Session: {self._escape_md(name)}",
                parse_mode="Markdown",
            )
        except Exception as e:
            await update.message.reply_text(f"Failed to attach: {e}")

    async def _handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle plain text messages - send as input to active session."""
        text = update.message.text
        session_id = self._active_session_id

        if not session_id:
            await update.message.reply_text(
                "No active session. Use /status to see sessions and /switch to select one."
            )
            return

        try:
            await self._agent.send_input(session_id, text)
            await update.message.reply_text("✓ Input sent")
        except Exception as e:
            await update.message.reply_text(f"Failed to send input: {e}")

    def _resolve_session_id(self, identifier: str) -> str | None:
        """Resolve a session identifier (ID or number) to session ID."""
        if identifier in self._sessions:
            return identifier

        try:
            index = int(identifier) - 1
            session_ids = list(self._sessions.keys())
            if 0 <= index < len(session_ids):
                return session_ids[index]
        except ValueError:
            pass

        for sid in self._sessions:
            if sid.startswith(identifier):
                return sid

        return None

    @staticmethod
    def _state_emoji(state: str) -> str:
        """Map session state to emoji."""
        return {
            "CREATED": "🆕",
            "RUNNING": "🔄",
            "AWAITING_INPUT": "📝",
            "INTERRUPTING": "⏳",
            "ERROR": "❌",
        }.get(state, "❓")

    @staticmethod
    def _escape_md(text: str) -> str:
        """Escape Markdown special characters."""
        for char in [
            "_",
            "*",
            "[",
            "]",
            "(",
            ")",
            "~",
            "`",
            ">",
            "#",
            "+",
            "-",
            "=",
            "|",
            "{",
            "}",
            ".",
            "!",
        ]:
            text = text.replace(char, f"\\{char}")
        return text
