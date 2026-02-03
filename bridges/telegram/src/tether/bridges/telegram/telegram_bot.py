"""Telegram bot handlers and bridge logic."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

if TYPE_CHECKING:
    from tether.bridges.telegram.config import Config
    from tether.bridges.telegram.sse_client import AgentClient
    from tether.bridges.telegram.state import StateManager

logger = logging.getLogger(__name__)


class TelegramBridge:
    """Telegram bot that bridges to Tether agent using forum topics.

    Each attached session gets its own forum topic. The General topic serves
    as the control plane for commands like /attach, /external, /status, /help.

    Subscribes to agent SSE events and sends Telegram notifications when
    input is required. Routes user replies back to the agent as session input.

    Args:
        config: Bridge configuration with bot token and forum group ID.
        agent: HTTP client for agent communication.
        state: Persistent state manager for session↔topic mappings.
    """

    def __init__(self, config: "Config", agent: "AgentClient", state: "StateManager"):
        self._config = config
        self._agent = agent
        self._state = state
        self._app: Application | None = None
        self._sessions: dict[str, dict] = {}
        self._external_sessions: list[dict] = []
        self._subscriptions: dict[str, asyncio.Task] = {}
        self._output_buffers: dict[str, list[str]] = {}
        self._pending_permissions: dict[str, dict] = {}

    async def start(self) -> None:
        """Start the Telegram bot."""
        self._app = Application.builder().token(self._config.telegram_bot_token).build()

        # Verify bot has required permissions
        await self._verify_forum_permissions()

        self._app.add_handler(CommandHandler("status", self._handle_status))
        self._app.add_handler(CommandHandler("sessions", self._handle_status))
        self._app.add_handler(CommandHandler("stop", self._handle_stop))
        self._app.add_handler(CommandHandler("external", self._handle_external))
        self._app.add_handler(CommandHandler("attach", self._handle_attach))
        self._app.add_handler(CommandHandler("help", self._handle_help))
        self._app.add_handler(CommandHandler("start", self._handle_help))
        self._app.add_handler(CallbackQueryHandler(self._handle_permission_callback, pattern="^perm:"))
        self._app.add_handler(
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND
                & filters.Chat(self._config.telegram_forum_group_id),
                self._handle_message,
            )
        )

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

        await self._refresh_sessions()
        logger.info("Telegram bot started")

    async def _verify_forum_permissions(self) -> None:
        """Verify the bot has required forum permissions."""
        chat = await self._app.bot.get_chat(self._config.telegram_forum_group_id)
        if not chat.is_forum:
            raise ValueError(
                "Chat must be a forum (supergroup with topics enabled). "
                "Enable topics in your group settings."
            )

        me = await self._app.bot.get_me()
        member = await self._app.bot.get_chat_member(
            self._config.telegram_forum_group_id, me.id
        )

        if not hasattr(member, "can_manage_topics") or not member.can_manage_topics:
            raise ValueError(
                "Bot must have can_manage_topics permission. "
                "Make the bot an admin with 'Manage Topics' right."
            )

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
            return

        elif event_type == "session_state":
            state = data.get("state")
            session = self._sessions.get(session_id)
            if session:
                session["state"] = state

            if state == "ERROR":
                session_name = session.get("name") if session else session_id
                topic_id = self._state.get_topic_for_session(session_id)
                if topic_id:
                    await self._send_message(
                        f"❌ Session error: {session_name}", topic_id=topic_id
                    )
        elif event_type in ("output", "output_final"):
            await self._handle_output_event(session_id, data, event_type)
        elif event_type == "permission_request":
            await self._handle_permission_request(session_id, data)

    async def _notify_input_required(self, session_id: str, data: dict) -> None:
        """Send Telegram notification that input is required."""
        session = self._sessions.get(session_id)
        session_name = (
            data.get("session_name")
            or (session.get("name") if session else None)
            or session_id[:12]
        )
        last_output = data.get("last_output") or ""

        # Get or create topic for this session
        topic_id = await self._get_or_create_topic(session_id, session_name)

        lines = ["📝 *Input Required*"]
        if last_output:
            truncated = (
                last_output[:400] + "..." if len(last_output) > 400 else last_output
            )
            lines.append(f"\n```\n{truncated}\n```")
        lines.append("\n_Reply here to send input_")

        await self._send_message(
            "\n".join(lines),
            topic_id=topic_id,
            parse_mode="MarkdownV2",
        )

    async def _send_message(
        self, text: str, topic_id: int | None = None, **kwargs
    ):
        """Send a message to General (topic_id=None) or a specific topic."""
        if self._app:
            return await self._app.bot.send_message(
                chat_id=self._config.telegram_forum_group_id,
                message_thread_id=topic_id,
                text=text,
                **kwargs,
            )
        return None

    async def _handle_permission_request(self, session_id: str, data: dict) -> None:
        """Send a permission request prompt with inline approve/deny buttons."""
        topic_id = self._state.get_topic_for_session(session_id)
        if not topic_id:
            return

        request_id = data.get("request_id")
        tool_name = data.get("tool_name") or "unknown"
        tool_input = data.get("tool_input") or {}
        if not request_id:
            return

        short_id = secrets.token_hex(4)
        self._pending_permissions[short_id] = {
            "session_id": session_id,
            "request_id": request_id,
        }

        try:
            tool_input_text = json.dumps(tool_input, indent=2, ensure_ascii=True)
        except TypeError:
            tool_input_text = str(tool_input)

        if len(tool_input_text) > 1200:
            tool_input_text = tool_input_text[:1200] + "..."

        text = (
            "Permission requested\n"
            f"Tool: {tool_name}\n"
            f"Input:\n{tool_input_text}"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Approve", callback_data=f"perm:{short_id}:allow"
                    ),
                    InlineKeyboardButton("Deny", callback_data=f"perm:{short_id}:deny"),
                ]
            ]
        )

        await self._send_message(text, topic_id=topic_id, reply_markup=keyboard)

    async def _handle_permission_callback(
        self, update: Update, _: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle inline approve/deny callbacks for permission requests."""
        query = update.callback_query
        if not query or not query.data:
            return

        await query.answer()
        parts = query.data.split(":")
        if len(parts) != 3:
            return

        _, short_id, action = parts
        pending = self._pending_permissions.pop(short_id, None)
        if not pending:
            await query.answer("Permission request no longer available", show_alert=True)
            return

        session_id = pending["session_id"]
        request_id = pending["request_id"]
        allow = action == "allow"

        try:
            await self._agent.respond_permission(
                session_id=session_id,
                request_id=request_id,
                allow=allow,
            )
            status = "✅ Approved" if allow else "❌ Denied"
            await query.edit_message_text(f"{query.message.text}\n\n{status}")
        except Exception as e:
            await query.edit_message_text(f"{query.message.text}\n\nFailed: {e}")

    async def _handle_output_event(
        self, session_id: str, data: dict, event_type: str
    ) -> None:
        """Aggregate output events and send the final response to Telegram."""
        text = data.get("text") or ""
        if not text.strip():
            return

        buffer = self._output_buffers.setdefault(session_id, [])
        buffer.append(text)

        is_final = data.get("final")
        if is_final is None and data.get("kind") == "final":
            is_final = True
        if event_type == "output_final":
            is_final = True

        if not is_final:
            return

        full_text = "".join(buffer).strip()
        self._output_buffers.pop(session_id, None)
        if not full_text:
            return

        topic_id = self._state.get_topic_for_session(session_id)
        if not topic_id:
            return

        for chunk in self._chunk_text(full_text):
            await self._send_message(chunk, topic_id=topic_id)

    @staticmethod
    def _chunk_text(text: str, limit: int = 4000) -> list[str]:
        """Split a message into Telegram-safe chunk sizes."""
        return [text[i : i + limit] for i in range(0, len(text), limit)]

    async def _create_topic_for_session(self, session_id: str, name: str) -> int:
        """Create a forum topic for a session, return topic_id."""
        topic = await self._app.bot.create_forum_topic(
            chat_id=self._config.telegram_forum_group_id,
            name=name[:128],  # Telegram limit
            icon_color=7322096,  # Light blue
        )
        self._state.set_topic_for_session(session_id, topic.message_thread_id, name)
        logger.info("Created topic %s for session %s", topic.message_thread_id, session_id)
        return topic.message_thread_id

    async def _get_or_create_topic(self, session_id: str, name: str) -> int:
        """Get existing topic or create new one."""
        topic_id = self._state.get_topic_for_session(session_id)
        if topic_id:
            return topic_id
        return await self._create_topic_for_session(session_id, name)

    async def _handle_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /help and /start commands."""
        if not self._is_general_topic(update):
            await update.message.reply_text(
                self._escape_md("Use /help in the General topic."),
                parse_mode="MarkdownV2",
            )
            return

        help_text = """*Tether Telegram Bridge*

Commands \\(use in General topic\\):
/status \\- List all sessions and their topics
/external \\- List external sessions \\(Claude Code, Codex\\)
/attach <id> \\- Attach to an external session \\(creates a topic\\)
/stop [id] \\- Interrupt a session
/help \\- Show this help

Each attached session gets its own topic\\. Send messages in a session's topic to interact with it\\.
"""
        await update.message.reply_text(help_text, parse_mode="MarkdownV2")

    async def _handle_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /status command."""
        if not self._is_general_topic(update):
            await update.message.reply_text(
                self._escape_md("Use /status in the General topic."),
                parse_mode="MarkdownV2",
            )
            return

        await self._refresh_sessions()

        if not self._sessions:
            await update.message.reply_text("No sessions found.")
            return

        lines = ["*Sessions:*"]
        for i, (sid, session) in enumerate(self._sessions.items(), 1):
            state = session.get("state", "UNKNOWN")
            name = session.get("name") or sid[:12]
            state_emoji = self._state_emoji(state)
            topic_id = self._state.get_topic_for_session(sid)
            topic_indicator = "→ topic" if topic_id else "no topic"
            directory = session.get("directory") or ""
            dir_label = directory.split("/")[-1] if directory else "unknown"
            lines.append(
                f"{i}\\. {state_emoji} {self._escape_md(name)} "
                f"{topic_indicator} dir: `{self._escape_md(dir_label)}`"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

    async def _handle_stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /stop command (interrupts the current turn)."""
        args = context.args
        topic_id = update.message.message_thread_id

        # If in a topic, use that topic's session; otherwise require an argument
        if topic_id:
            session_id = self._state.get_session_for_topic(topic_id)
        elif args:
            session_id = self._resolve_session_id(args[0])
        else:
            session_id = None

        if not session_id:
            await update.message.reply_text(
                "Usage: /stop <session\\_id or number>\n\n"
                "Or use /stop in a session's topic.",
                parse_mode="MarkdownV2",
            )
            return

        try:
            await self._agent.interrupt_session(session_id)
            session = self._sessions.get(session_id)
            name = session.get("name") if session else session_id[:12]
            await update.message.reply_text(f"⏹️ Interrupted: {name}")
        except Exception as e:
            await update.message.reply_text(f"Failed to interrupt session: {e}")

    async def _handle_external(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /external command - list external sessions."""
        if not self._is_general_topic(update):
            await update.message.reply_text(
                self._escape_md("Use /external in the General topic."),
                parse_mode="MarkdownV2",
            )
            return

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
                f"{i}\\. {is_running} *{self._escape_md(runner)}* in `{self._escape_md(dir_short)}`"
            )
            if prompt_short:
                lines.append(f"   _{self._escape_md(prompt_short)}_")

        lines.append(self._escape_md("\nUse /attach <number> to attach to a session."))
        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

    async def _handle_attach(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /attach command - attach to an external session."""
        if not self._is_general_topic(update):
            await update.message.reply_text(
                self._escape_md("Use /attach in the General topic."),
                parse_mode="MarkdownV2",
            )
            return

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
            if not session_id:
                await update.message.reply_text("Failed to attach: no session ID returned")
                return

            self._sessions[session_id] = result
            self._ensure_subscription(session_id)

            # Create topic for the new session
            name = result.get("name") or f"session-{session_id[:8]}"
            topic_id = await self._create_topic_for_session(session_id, name)

            # Announce in General
            dir_short = directory.split("/")[-1] if directory else "unknown"
            await update.message.reply_text(
                f"✅ Attached to {self._escape_md(runner_type)} session: {self._escape_md(name)}\n"
                f"Directory: `{self._escape_md(dir_short)}`\n\n"
                "A topic has been created for this session\\.",
                parse_mode="MarkdownV2",
            )

            # Pin session metadata in new topic
            runner_label = runner_type or "unknown"
            dir_label = directory or "unknown"
            pin_text = (
                f"*Session Attached*\n"
                f"Session: {self._escape_md(name)}\n"
                f"Runner: {self._escape_md(runner_label)}\n"
                f"Directory: `{self._escape_md(dir_label)}`"
            )
            pin_msg = await self._send_message(
                pin_text,
                topic_id=topic_id,
                parse_mode="MarkdownV2",
            )
            if pin_msg and self._app:
                await self._app.bot.pin_chat_message(
                    chat_id=self._config.telegram_forum_group_id,
                    message_thread_id=topic_id,
                    message_id=pin_msg.message_id,
                    disable_notification=True,
                )
        except Exception as e:
            await update.message.reply_text(f"Failed to attach: {e}")

    async def _handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle plain text messages - route to session based on topic."""
        text = update.message.text
        topic_id = update.message.message_thread_id

        if topic_id is None:
            # Message in General - not in any topic
            await update.message.reply_text(
                "Use /help for commands. Chat in session topics to interact with agents."
            )
            return

        # Find session for this topic
        session_id = self._state.get_session_for_topic(topic_id)
        if not session_id:
            await update.message.reply_text(
                "This topic is not linked to a session. Use /attach in General to create sessions."
            )
            return

        try:
            await self._agent.send_input(session_id, text)
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

    @staticmethod
    def _is_general_topic(update: Update) -> bool:
        """Return True if the message is in the General topic."""
        return update.message.message_thread_id is None
