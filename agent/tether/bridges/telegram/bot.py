"""Telegram bot bridge implementation."""

import asyncio
import json
import os
from typing import Any

import structlog

from tether.bridges.base import (
    ApprovalRequest,
    BridgeInterface,
    _EXTERNAL_MAX_FETCH,
    _EXTERNAL_REPLAY_LIMIT,
)
from tether.bridges.telegram.formatting import (
    chunk_message,
    escape_markdown,
    markdown_to_telegram_html,
    strip_tool_markers,
)
from tether.bridges.telegram.state import StateManager
from tether.settings import settings

logger = structlog.get_logger(__name__)

_TELEGRAM_TOPIC_NAME_MAX_LEN = 64
_APPROVAL_TRUNCATE = 120  # max chars per value in compact approval view


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
        super().__init__()
        self._bot_token = bot_token
        self._forum_group_id = forum_group_id
        self._app: Any = None
        self._state = state_manager or StateManager(
            os.path.join(settings.data_dir(), "telegram_state.json")
        )
        self._state.load()
        # Cache full approval descriptions for "Show All" button: request_id → (tool, full_html)
        self._pending_descriptions: dict[str, tuple[str, str]] = {}
        # Cache original HTML text for approval messages: request_id → html
        self._approval_html: dict[str, str] = {}
        # Pending "Deny with reason" state: topic_id → (session_id, request_id, username)
        self._pending_deny_reason: dict[int, tuple[str, str, str]] = {}
        # Background typing indicator loops: session_id → asyncio.Task
        self._typing_tasks: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        """Start the Telegram bot."""
        try:
            from telegram.ext import (
                Application,
                CallbackQueryHandler,
                CommandHandler,
                MessageHandler,
                filters,
            )
        except ImportError:
            logger.error(
                "python-telegram-bot not installed. Install with: pip install python-telegram-bot"
            )
            return

        self._app = Application.builder().token(self._bot_token).build()

        # Command handlers
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("start", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("sessions", self._cmd_status))
        self._app.add_handler(CommandHandler("list", self._cmd_list))
        self._app.add_handler(CommandHandler("attach", self._cmd_attach))
        self._app.add_handler(CommandHandler("new", self._cmd_new))
        self._app.add_handler(CommandHandler("stop", self._cmd_stop))
        self._app.add_handler(CommandHandler("usage", self._cmd_usage))

        # Plain text handler for human input (in session topics)
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.SUPERGROUP,
                self._handle_message,
            )
        )

        # External session pagination handler
        self._app.add_handler(
            CallbackQueryHandler(self._handle_list_callback_query, pattern=r"^list:")
        )

        # Approval button handler
        self._app.add_handler(
            CallbackQueryHandler(self._handle_callback_query, pattern=r"^approval:")
        )

        await self._app.initialize()

        # Register command menu with Telegram
        from telegram import BotCommand

        await self._app.bot.set_my_commands(
            [
                BotCommand("status", "List all sessions"),
                BotCommand("list", "List external sessions (Claude Code, Codex)"),
                BotCommand("attach", "Attach to an external session"),
                BotCommand("new", "Start a new session"),
                BotCommand("stop", "Interrupt the session in this topic"),
                BotCommand("usage", "Show token usage and cost"),
                BotCommand("help", "Show available commands"),
            ]
        )

        await self._app.start()
        await self._app.updater.start_polling()
        logger.info(
            "Telegram bridge initialized and started",
            forum_group_id=self._forum_group_id,
        )

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        if self._app:
            if self._app.updater.running:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        logger.info("Telegram bridge stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _display_name(user: Any) -> str:
        """Get a human-readable display name from a Telegram user object."""
        if not user:
            return "unknown"
        if user.username:
            return f"@{user.username}"
        parts = [user.first_name or "", user.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        return name or "unknown"

    @staticmethod
    def _format_tool_input_html(
        raw: str, *, truncate: int = _APPROVAL_TRUNCATE
    ) -> tuple[str, bool]:
        """Pretty-format tool_input as Telegram HTML.

        Returns (html_text, was_truncated).
        """
        import html as html_mod

        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            obj = None

        truncated = False

        if isinstance(obj, dict):
            lines: list[str] = []
            for key, value in obj.items():
                label = BridgeInterface._humanize_key(str(key))
                v = BridgeInterface._humanize_enum_value(value)
                if len(v) > truncate:
                    v = v[:truncate] + "…"
                    truncated = True
                v_escaped = html_mod.escape(v)
                label_escaped = html_mod.escape(label)
                if key in ("file_path", "path", "notebook_path"):
                    lines.append(
                        f"<b>{label_escaped}</b>: <code>{v_escaped}</code>"
                    )
                elif key in ("command",):
                    lines.append(
                        f"<b>{label_escaped}</b>:\n<pre>{v_escaped}</pre>"
                    )
                elif key in ("old_string", "new_string", "content", "new_source"):
                    lines.append(
                        f"<b>{label_escaped}</b>:\n<pre>{v_escaped}</pre>"
                    )
                else:
                    lines.append(f"<b>{label_escaped}</b>: {v_escaped}")
            return "\n".join(lines), truncated

        text = html_mod.escape(str(raw))
        if len(text) > truncate * 3:
            text = text[: truncate * 3] + "…"
            truncated = True
        return text, truncated

    @staticmethod
    def _format_tool_input_full_html(raw: str) -> str:
        """Format tool_input as Telegram HTML without truncation."""
        import html as html_mod

        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            obj = None

        if isinstance(obj, dict):
            lines: list[str] = []
            for key, value in obj.items():
                label = BridgeInterface._humanize_key(str(key))
                v = BridgeInterface._humanize_enum_value(value)
                v_escaped = html_mod.escape(v)
                label_escaped = html_mod.escape(label)
                if key in ("file_path", "path", "notebook_path"):
                    lines.append(
                        f"<b>{label_escaped}</b>: <code>{v_escaped}</code>"
                    )
                elif key in (
                    "command",
                    "old_string",
                    "new_string",
                    "content",
                    "new_source",
                ):
                    lines.append(
                        f"<b>{label_escaped}</b>:\n<pre>{v_escaped}</pre>"
                    )
                else:
                    lines.append(f"<b>{label_escaped}</b>: {v_escaped}")
            return "\n".join(lines)

        return html_mod.escape(str(raw))

    def _make_external_topic_name(self, *, directory: str, session_id: str) -> str:
        """Generate a topic name from the directory, UpperCased.

        If a topic with the same name already exists, append a number.
        """
        dir_short = (directory or "").rstrip("/").rsplit("/", 1)[-1] or "Session"
        base_name = (dir_short[:1].upper() + dir_short[1:])[
            :_TELEGRAM_TOPIC_NAME_MAX_LEN
        ]

        # Check existing topic names for duplicates
        existing_names = {m.name for m in self._state._mappings.values()}

        if base_name not in existing_names:
            return base_name

        for i in range(2, 100):
            candidate = f"{base_name} {i}"[:_TELEGRAM_TOPIC_NAME_MAX_LEN]
            if candidate not in existing_names:
                return candidate

        return base_name

    async def _send_external_session_replay(
        self,
        *,
        topic_id: int,
        external_id: str,
        runner_type: str,
        limit: int = _EXTERNAL_REPLAY_LIMIT,
    ) -> None:
        """Send recent external session history into the Telegram topic.

        Sends a single consolidated message (chunked if needed) to avoid
        flooding the topic with many small messages.
        """
        if not self._app:
            return

        import httpx

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self._api_url(f"/external-sessions/{external_id}/history"),
                    headers=self._api_headers(),
                    params={"runner_type": runner_type, "limit": limit},
                    timeout=10.0,
                )
                response.raise_for_status()
            payload = response.json()
        except Exception:
            logger.exception(
                "Failed to fetch external session history for replay",
                external_id=external_id,
                runner_type=runner_type,
            )
            return

        messages = payload.get("messages") or []
        if not messages:
            return

        blocks: list[str] = [f"📜 Replaying last {len(messages)} messages:"]
        for msg in messages:
            role = str(msg.get("role") or "").lower()
            content = strip_tool_markers((msg.get("content") or "").strip())
            thinking = (msg.get("thinking") or "").strip()

            if not content and not thinking:
                continue

            if role == "user":
                prefix = "👤"
            elif role == "assistant":
                prefix = "🤖"
            else:
                prefix = "💬"

            parts: list[str] = [prefix]
            if thinking:
                truncated = thinking[:400] + "..." if len(thinking) > 400 else thinking
                parts.append(f"💭 {truncated}")
            if content:
                truncated = content[:800] + "..." if len(content) > 800 else content
                parts.append(truncated)

            blocks.append("\n\n".join(parts))

        text = "\n\n".join(blocks)
        html_text = markdown_to_telegram_html(text)

        first_msg_id: int | None = None
        for part in chunk_message(html_text):
            try:
                sent = await self._app.bot.send_message(
                    chat_id=self._forum_group_id,
                    message_thread_id=topic_id,
                    text=part,
                    parse_mode="HTML",
                )
                if first_msg_id is None:
                    first_msg_id = getattr(sent, "message_id", None)
            except Exception:
                # Fallback to plain text if HTML fails
                try:
                    sent = await self._app.bot.send_message(
                        chat_id=self._forum_group_id,
                        message_thread_id=topic_id,
                        text=part.replace("<pre>", "")
                        .replace("</pre>", "")
                        .replace("<b>", "")
                        .replace("</b>", "")
                        .replace("<i>", "")
                        .replace("</i>", "")
                        .replace("<code>", "")
                        .replace("</code>", ""),
                    )
                    if first_msg_id is None:
                        first_msg_id = getattr(sent, "message_id", None)
                except Exception:
                    logger.exception(
                        "Failed to send replay message",
                        external_id=external_id,
                        topic_id=topic_id,
                    )

        # Telegram auto-pins the first message in a topic — undo that
        if first_msg_id:
            try:
                await self._app.bot.unpin_chat_message(
                    chat_id=self._forum_group_id,
                    message_id=first_msg_id,
                )
            except Exception:
                pass  # Not critical

    async def _refresh_external_cache(self) -> None:
        """Refresh cached external session list from the API."""
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(
                self._api_url("/external-sessions"),
                headers=self._api_headers(),
                params={"limit": _EXTERNAL_MAX_FETCH},
                timeout=10.0,
            )
            response.raise_for_status()
        self._cached_external = response.json()

    def _external_pagination_markup(self, page: int, total_pages: int):
        """Build inline keyboard markup for external session pagination."""
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except ImportError:
            return None

        if total_pages <= 1:
            return None

        buttons = []
        if page > 1:
            buttons.append(
                InlineKeyboardButton("Prev", callback_data=f"list:page:{page - 1}")
            )
        buttons.append(InlineKeyboardButton("Refresh", callback_data="list:refresh"))
        if page < total_pages:
            buttons.append(
                InlineKeyboardButton("Next", callback_data=f"list:page:{page + 1}")
            )
        return InlineKeyboardMarkup([buttons])

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _cmd_help(self, update: Any, context: Any) -> None:
        """Handle /help and /start commands."""
        text = (
            "Tether Bot Commands:\n\n"
            "/status — List all sessions\n"
            "/list [page|search] — List external sessions (Claude Code, Codex)\n"
            "/attach <number> [force] — Attach to an external session\n"
            "/new [agent] [directory] — Start a new session\n"
            "/stop — Interrupt the session in this topic\n"
            "/usage — Show token usage and cost for this session\n"
            "/help — Show this help\n\n"
            "Send a text message in a session topic to forward it as input."
        )
        await update.message.reply_text(text)

    async def _cmd_status(self, update: Any, context: Any) -> None:
        """Handle /status — list all Tether sessions."""
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self._api_url("/sessions"),
                    headers=self._api_headers(),
                    timeout=10.0,
                )
                response.raise_for_status()
            sessions = response.json()
        except Exception:
            logger.exception("Failed to fetch sessions for /status")
            await update.message.reply_text("Failed to fetch sessions.")
            return

        if not sessions:
            await update.message.reply_text("No sessions.")
            return

        lines = ["Sessions:\n"]
        for s in sessions:
            emoji = self._STATE_EMOJI.get(s.get("state", ""), "❓")
            name = s.get("name") or s.get("id", "")[:12]
            lines.append(f"  {emoji} {name}")
        await update.message.reply_text("\n".join(lines))

    async def _cmd_list(self, update: Any, context: Any) -> None:
        """Handle /list — list external sessions available for attachment."""
        page = 1
        query: str | None = None
        args = getattr(context, "args", None) or []
        if args:
            first = args[0]
            try:
                page = int(first)
                # Keep existing search (if any) when navigating by page number.
                query = self._external_query
            except Exception:
                query = " ".join(args).strip()
                page = 1

        try:
            await self._refresh_external_cache()
            # If no args, clear the search.
            if not args:
                self._set_external_view(None)
            else:
                self._set_external_view(query)
        except Exception:
            logger.exception("Failed to fetch external sessions")
            await update.message.reply_text("Failed to list external sessions.")
            return

        text, page, total_pages = self._format_external_page(
            page, attach_cmd="/attach", list_cmd="/list"
        )
        reply_markup = self._external_pagination_markup(page, total_pages)
        await update.message.reply_text(text, reply_markup=reply_markup)

    async def _cmd_attach(self, update: Any, context: Any) -> None:
        """Handle /attach <number> [force] — attach to an external session and create a topic."""
        import httpx

        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: /attach <number> [force]\n\nRun /list first."
            )
            return

        # Parse optional "force" flag
        force = len(args) > 1 and args[1].lower() == "force"

        try:
            index = int(args[0]) - 1
        except ValueError:
            await update.message.reply_text("Please provide a session number.")
            return

        if not self._cached_external:
            await update.message.reply_text(
                "No external sessions cached. Run /list first."
            )
            return
        if not self._external_view:
            await update.message.reply_text(
                "No external sessions listed. Run /list first."
            )
            return
        if index < 0 or index >= len(self._external_view):
            await update.message.reply_text(
                f"Invalid number. Use 1–{len(self._external_view)}."
            )
            return

        external = self._external_view[index]

        try:
            # Create Tether session via attach endpoint
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self._api_url("/sessions/attach"),
                    json={
                        "external_id": external["id"],
                        "runner_type": external["runner_type"],
                        "directory": external["directory"],
                    },
                    headers=self._api_headers(),
                    timeout=30.0,
                )
                response.raise_for_status()
            session = response.json()
            session_id = session["id"]

            # Check if this session already has a topic
            existing_topic = self._state.get_topic_for_session(session_id)
            if existing_topic:
                if force:
                    logger.info(
                        "Force-recreating topic",
                        session_id=session_id,
                        topic_id=existing_topic,
                    )
                    self._state.remove_session(session_id)
                else:
                    # Verify the topic is still usable by sending a test message
                    topic_ok = False
                    try:
                        test_msg = await self._app.bot.send_message(
                            chat_id=self._forum_group_id,
                            message_thread_id=existing_topic,
                            text="Reconnected.",
                        )
                        await test_msg.delete()
                        topic_ok = True
                    except Exception:
                        logger.info(
                            "Existing topic is stale, will recreate",
                            session_id=session_id,
                            topic_id=existing_topic,
                        )
                        self._state.remove_session(session_id)

                    if topic_ok:
                        await update.message.reply_text(
                            "Already attached — check the existing topic.\n"
                            "Use /attach <number> force to recreate the topic."
                        )
                        return

            # Create forum topic
            session_name = self._make_external_topic_name(
                directory=external.get("directory", ""),
                session_id=session_id,
            )
            thread_info = await self.create_thread(session_id, session_name)
            try:
                topic_id = int(thread_info.get("topic_id") or 0)
                if topic_id:
                    await self._send_external_session_replay(
                        topic_id=topic_id,
                        external_id=external["id"],
                        runner_type=str(external["runner_type"]),
                    )
            except Exception:
                # Replay is best-effort; it should never block attachment.
                logger.exception(
                    "Failed to replay external session history into Telegram topic"
                )

            # Bind session to Telegram platform
            from tether.store import store
            from tether.bridges.subscriber import bridge_subscriber

            db_session = store.get_session(session_id)
            if db_session:
                db_session.platform = "telegram"
                db_session.platform_thread_id = thread_info.get("thread_id")
                store.update_session(db_session)

            bridge_subscriber.subscribe(session_id, "telegram")

            dir_short = external.get("directory", "").rsplit("/", 1)[-1]
            await update.message.reply_text(
                f"✅ Attached to {external['runner_type']} session in {dir_short}\n\n"
                f"A new topic has been created — send messages there to interact."
            )

        except httpx.HTTPStatusError as e:
            await update.message.reply_text(f"Failed to attach: {e.response.text}")
        except Exception as e:
            logger.exception("Failed to attach to external session")
            await update.message.reply_text(f"Failed to attach: {e}")

    async def _cmd_new(self, update: Any, context: Any) -> None:
        """Handle /new — create a new session and topic.

        Usage:
        - In a session topic:
          - /new
          - /new <agent>
          - /new <directory-name>
        - In General (or any non-session topic):
          - /new <agent> <directory>
          - /new <directory>
        """
        import httpx

        args = getattr(context, "args", None) or []
        topic_id = update.message.message_thread_id

        base_session_id: str | None = None
        base_directory: str | None = None
        base_adapter: str | None = None
        if topic_id:
            base_session_id = self._state.get_session_for_topic(topic_id)
        if base_session_id:
            from tether.store import store

            s = store.get_session(base_session_id)
            if s:
                base_directory = s.directory
                base_adapter = s.adapter

        adapter: str | None = None
        directory_raw: str | None = None

        if not args:
            if not base_directory:
                await update.message.reply_text(
                    "Usage: /new <agent> <directory>\n"
                    "Or, inside a session topic: /new or /new <agent>"
                )
                return
            adapter = base_adapter
            directory_raw = base_directory
        elif len(args) == 1:
            token = args[0].strip()
            maybe_adapter = self._agent_to_adapter(token)
            if base_directory:
                if maybe_adapter:
                    adapter = maybe_adapter
                    directory_raw = base_directory
                else:
                    adapter = base_adapter
                    directory_raw = token
            else:
                # Non-session topic: allow /new <directory> (default adapter)
                if maybe_adapter:
                    await update.message.reply_text("Usage: /new <agent> <directory>")
                    return
                directory_raw = token
        else:
            adapter = self._agent_to_adapter(args[0])
            if not adapter:
                await update.message.reply_text(
                    "Unknown agent. Use: claude, codex, claude_auto, claude_local, claude_api, codex_sdk_sidecar"
                )
                return
            directory_raw = " ".join(args[1:]).strip()

        try:
            assert directory_raw is not None
            directory = await self._resolve_directory_arg(
                directory_raw,
                base_directory=base_directory,
            )
        except Exception as e:
            await update.message.reply_text(f"Invalid directory: {e}")
            return

        dir_short = directory.rstrip("/").rsplit("/", 1)[-1] or "Session"
        session_name = self._make_external_topic_name(
            directory=directory, session_id=""
        )  # best-effort uniqueness

        try:
            session = await self._create_session_via_api(
                directory=directory,
                platform="telegram",
                adapter=adapter,
                session_name=session_name,
            )
            new_topic_id = int(session.get("platform_thread_id") or 0)
        except httpx.HTTPStatusError as e:
            await update.message.reply_text(
                f"Failed to create session: {e.response.text}"
            )
            return
        except Exception as e:
            logger.exception("Failed to create session via /new")
            await update.message.reply_text(f"Failed to create session: {e}")
            return

        # Confirm in the issuing topic.
        agent_label = self._adapter_label(adapter) or self._adapter_label(settings.adapter()) or "Claude"
        parts = [f"✅ New {agent_label} session created in {dir_short}."]
        if new_topic_id:
            # Telegram deep-link to the topic
            # Format: https://t.me/c/<chat_id_without_-100>/<topic_id>
            chat_id_str = str(self._forum_group_id)
            if chat_id_str.startswith("-100"):
                chat_id_str = chat_id_str[4:]
            link = f"https://t.me/c/{chat_id_str}/{new_topic_id}"
            parts.append(f'<a href="{link}">Open topic →</a>')
        else:
            parts.append("A new topic should appear in the forum list.")
        await update.message.reply_text("\n".join(parts), parse_mode="HTML")

        # Post a short intro in the new topic.
        if self._app and new_topic_id:
            try:
                import html as _html

                intro = await self._app.bot.send_message(
                    chat_id=self._forum_group_id,
                    message_thread_id=new_topic_id,
                    text=(
                        f"🆕 New session in <code>{_html.escape(directory)}</code>\n\n"
                        "Send a message here to start."
                    ),
                    parse_mode="HTML",
                )
                # Telegram auto-pins the first message in a topic — undo that.
                try:
                    await self._app.bot.unpin_chat_message(
                        chat_id=self._forum_group_id,
                        message_id=intro.message_id,
                    )
                except Exception:
                    pass
            except Exception:
                pass

    async def _cmd_stop(self, update: Any, context: Any) -> None:
        """Handle /stop — interrupt the session in the current topic."""
        import httpx

        topic_id = update.message.message_thread_id
        if not topic_id:
            await update.message.reply_text("Use this command inside a session topic.")
            return

        session_id = self._state.get_session_for_topic(topic_id)
        if not session_id:
            await update.message.reply_text("No session linked to this topic.")
            return

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self._api_url(f"/sessions/{session_id}/interrupt"),
                    headers=self._api_headers(),
                    timeout=10.0,
                )
                response.raise_for_status()
            await update.message.reply_text("⏹️ Session interrupted.")
        except httpx.HTTPStatusError as e:
            error = e.response.json().get("error", {}).get("message", str(e))
            await update.message.reply_text(f"Cannot interrupt: {error}")
        except Exception as e:
            logger.exception("Failed to interrupt session")
            await update.message.reply_text(f"Failed to interrupt: {e}")

    async def _cmd_usage(self, update: Any, context: Any) -> None:
        """Handle /usage — show token and cost usage for the session in this topic."""
        import httpx

        topic_id = update.message.message_thread_id
        if not topic_id:
            await update.message.reply_text("Use this command inside a session topic.")
            return

        session_id = self._state.get_session_for_topic(topic_id)
        if not session_id:
            await update.message.reply_text("No session linked to this topic.")
            return

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self._api_url(f"/sessions/{session_id}/usage"),
                    headers=self._api_headers(),
                    timeout=10.0,
                )
                response.raise_for_status()
            usage = response.json()

            input_t = usage.get("input_tokens", 0)
            output_t = usage.get("output_tokens", 0)
            cost = usage.get("total_cost_usd", 0.0)

            lines = [
                "📊 <b>Session Usage</b>",
                "",
                f"Input tokens:  <code>{input_t:,}</code>",
                f"Output tokens: <code>{output_t:,}</code>",
                f"Total tokens:  <code>{input_t + output_t:,}</code>",
            ]
            if cost > 0:
                lines.append(f"Cost: <code>${cost:.4f}</code>")
            else:
                lines.append("Cost: <i>not tracked</i>")

            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        except httpx.HTTPStatusError as e:
            error = e.response.json().get("error", {}).get("message", str(e))
            await update.message.reply_text(f"Failed to get usage: {error}")
        except Exception as e:
            logger.exception("Failed to get usage")
            await update.message.reply_text(f"Failed to get usage: {e}")

    # ------------------------------------------------------------------
    # Message and callback handlers
    # ------------------------------------------------------------------

    async def _handle_list_callback_query(self, update: Any, context: Any) -> None:
        """Handle pagination callbacks for /list."""
        query = update.callback_query
        if not query or not getattr(query, "data", None):
            return

        data = query.data
        await query.answer()

        if data == "list:refresh":
            try:
                await self._refresh_external_cache()
            except Exception:
                logger.exception("Failed to refresh external sessions")
                try:
                    await query.edit_message_text(
                        "Failed to refresh external sessions."
                    )
                except Exception:
                    pass
                return
            self._set_external_view(self._external_query)
            page = 1
        else:
            # list:page:<n>
            try:
                _, kind, value = data.split(":", 2)
                if kind != "page":
                    return
                page = int(value)
            except Exception:
                return

        # If we somehow lost cache (restart), try a refresh for best UX.
        if not self._cached_external:
            try:
                await self._refresh_external_cache()
            except Exception:
                logger.exception("Failed to fetch external sessions for pagination")
                try:
                    await query.edit_message_text(
                        "Failed to list external sessions. Run /list again."
                    )
                except Exception:
                    pass
                return
            self._set_external_view(self._external_query)

        text, page, total_pages = self._format_external_page(
            page, attach_cmd="/attach", list_cmd="/list"
        )
        reply_markup = self._external_pagination_markup(page, total_pages)
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup)
        except Exception:
            # If edit fails (message too old, etc.), send a new message.
            try:
                await query.message.reply_text(text, reply_markup=reply_markup)
            except Exception:
                logger.exception("Failed to send external pagination message")

    async def _handle_message(self, update: Any, context: Any) -> None:
        """Handle incoming text messages from Telegram and forward via internal API."""
        if not update.message or not update.message.text:
            return

        topic_id = update.message.message_thread_id
        if not topic_id:
            await update.message.reply_text(
                "💡 Send messages in a session topic to interact with that agent. "
                "This is the General topic — messages here aren't routed to any session."
            )
            return

        session_id = self._state.get_session_for_topic(topic_id)
        if not session_id:
            await update.message.reply_text(
                "⚠️ No active session is linked to this topic."
            )
            return

        text = update.message.text.strip()

        # Check for pending "Deny with reason" — intercept reply as denial reason
        pending = self._pending_deny_reason.pop(topic_id, None)
        if pending:
            p_session_id, p_request_id, p_username = pending
            reason = text
            message = f"Denied by {p_username}: {reason}"
            ok = await self._respond_to_permission(
                p_session_id,
                p_request_id,
                allow=False,
                message=message,
            )
            if ok:
                await update.message.reply_text(f"❌ {message}")
            else:
                await update.message.reply_text(
                    "❌ Failed to deny — request may have expired."
                )
            return

        # Pending choice request: allow replying with "1"/"2"/... or an exact label.
        pending_req = self.get_pending_permission(session_id)
        if pending_req and pending_req.kind == "choice":
            selected = self.parse_choice_text(session_id, text)
            if selected:
                try:
                    await self._send_input_or_start_via_api(
                        session_id=session_id, text=selected
                    )
                    self.clear_pending_permission(session_id)
                    await update.message.reply_text(f"✅ Selected: {selected}")
                except Exception:
                    logger.exception(
                        "Failed to forward choice selection",
                        session_id=session_id,
                        topic_id=topic_id,
                    )
                    await update.message.reply_text("Failed to send selection.")
                return

        try:
            import httpx

            await self._send_input_or_start_via_api(session_id=session_id, text=text)

            logger.info(
                "Forwarded human input from Telegram",
                session_id=session_id,
                topic_id=topic_id,
                username=update.message.from_user.username,
            )
        except httpx.HTTPStatusError as e:
            try:
                data = e.response.json()
                err = data.get("error") or {}
                code = err.get("code")
                message = err.get("message") or e.response.text
                if code == "RUNNER_UNAVAILABLE":
                    message = (
                        "Runner backend is not reachable. Start `codex-sdk-sidecar` and try again."
                    )
            except Exception:
                message = e.response.text
            await update.message.reply_text(f"Failed to send input: {message}")
        except Exception:
            logger.exception(
                "Failed to forward human input",
                session_id=session_id,
                topic_id=topic_id,
            )
            await update.message.reply_text("Failed to send input.")

    async def _handle_callback_query(self, update: Any, context: Any) -> None:
        """Handle approval button clicks in Telegram."""
        query = update.callback_query
        if not query:
            return

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

        # Use cached HTML to preserve formatting when editing the message
        original_html = self._approval_html.get(request_id, query.message.text)

        # Handle "Show All" — resend full untruncated description
        if option_selected == "ShowAll":
            cached = self._pending_descriptions.get(request_id)
            if cached:
                tool_name, raw_desc = cached
                full_html = self._format_tool_input_full_html(raw_desc)
                full_text = f"⚠️ <b>{tool_name}</b> (full)\n\n{full_html}"
                # Send as new message (don't replace — keep buttons on original)
                for part in chunk_message(full_text):
                    try:
                        await self._app.bot.send_message(
                            chat_id=self._forum_group_id,
                            message_thread_id=topic_id,
                            text=part,
                            parse_mode="HTML",
                        )
                    except Exception:
                        await self._app.bot.send_message(
                            chat_id=self._forum_group_id,
                            message_thread_id=topic_id,
                            text=part,
                        )
            else:
                await query.answer("Full content no longer available")
            return

        try:
            username = self._display_name(query.from_user)

            # Choice selection: send selected option as session input.
            if option_selected.startswith("Choose:"):
                pending_req = self.get_pending_permission(session_id)
                if (
                    not pending_req
                    or pending_req.request_id != request_id
                    or pending_req.kind != "choice"
                ):
                    await query.edit_message_text(
                        text=f"{original_html}\n\n❌ Request expired.",
                        parse_mode="HTML",
                    )
                    return

                try:
                    raw = option_selected.split(":", 1)[1]
                    idx = int(raw) - 1
                except Exception:
                    idx = -1

                if idx < 0 or idx >= len(pending_req.options):
                    await query.answer("Invalid option")
                    return

                selected = pending_req.options[idx]
                await self._send_input_or_start_via_api(
                    session_id=session_id, text=selected
                )
                self.clear_pending_permission(session_id)
                await query.edit_message_text(
                    text=f"{original_html}\n\n✅ {selected} by {username}",
                    parse_mode="HTML",
                )
                return

            # Handle "Deny ✏️" — prompt for reason, don't resolve yet
            if option_selected == "DenyWithReason":
                self._pending_deny_reason[topic_id] = (session_id, request_id, username)
                await query.edit_message_text(
                    text=f"{original_html}\n\n✏️ Why? Reply with your reason.",
                    parse_mode="HTML",
                )
                return

            # Handle "Allow All (30m)", "Allow {tool} (30m)", "Allow dir (30m)"
            if option_selected == "AllowAll":
                self.set_allow_all(session_id)
                allow = True
                display_option = "Allow All (30m)"
            elif option_selected == "AllowDir":
                from tether.store import store as _store

                _sess = _store.get_session(session_id)
                if _sess and _sess.directory:
                    self.set_allow_directory(_sess.directory)
                    dir_short = _sess.directory.rstrip("/").rsplit("/", 1)[-1] or "dir"
                    display_option = f"Allow {dir_short}/ (30m)"
                else:
                    # Fallback to session-level allow-all
                    self.set_allow_all(session_id)
                    display_option = "Allow All (30m)"
                allow = True
            elif option_selected.startswith("AllowTool:"):
                tool_name = option_selected.split(":", 1)[1]
                self.set_allow_tool(session_id, tool_name)
                allow = True
                display_option = f"Allow {tool_name} (30m)"
            else:
                allow = option_selected.lower() in ("allow", "yes", "approve")
                display_option = option_selected

            message = f"{display_option} by {username}"
            if not allow:
                message = f"Denied by {username}"

            ok = await self._respond_to_permission(
                session_id,
                request_id,
                allow=allow,
                message=message,
            )
            if ok:
                if allow:
                    await query.edit_message_text(
                        text=f"{original_html}\n\n✅ {display_option} by {username}",
                        parse_mode="HTML",
                    )
                else:
                    await query.edit_message_text(
                        text=f"{original_html}\n\n❌ Denied by {username}",
                        parse_mode="HTML",
                    )
            else:
                await query.edit_message_text(
                    text=f"{original_html}\n\n❌ Error: Failed to submit response",
                    parse_mode="HTML",
                )

            # Clean up cached HTML
            self._approval_html.pop(request_id, None)

            logger.info(
                "Approval response submitted",
                session_id=session_id,
                request_id=request_id,
                option=display_option,
                username=username,
            )

        except Exception:
            logger.exception(
                "Failed to handle callback",
                session_id=session_id,
                request_id=request_id,
            )
            await query.edit_message_text(
                text=f"{original_html}\n\n❌ Error: Failed to submit response",
                parse_mode="HTML",
            )

    # ------------------------------------------------------------------
    # Bridge interface (outgoing events)
    # ------------------------------------------------------------------

    async def on_output(
        self, session_id: str, text: str, metadata: dict | None = None
    ) -> None:
        """Send output text to the session's Telegram topic."""
        self._stop_typing(session_id)
        if not self._app:
            logger.warning("Telegram app not initialized")
            return

        topic_id = self._state.get_topic_for_session(session_id)
        if not topic_id:
            logger.warning("No Telegram topic for session", session_id=session_id)
            return

        formatted = markdown_to_telegram_html(text)
        chunks = chunk_message(formatted)
        for chunk in chunks:
            try:
                await self._app.bot.send_message(
                    chat_id=self._forum_group_id,
                    message_thread_id=topic_id,
                    text=chunk,
                    parse_mode="HTML",
                )
            except Exception:
                # Fallback to plain text if HTML parsing fails
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

    async def send_auto_approve_batch(
        self, session_id: str, items: list[tuple[str, str]]
    ) -> None:
        """Send a batched auto-approve notification to Telegram."""
        if not self._app:
            return
        topic_id = self._state.get_topic_for_session(session_id)
        if not topic_id:
            return

        if len(items) == 1:
            tool_name, reason = items[0]
            text = f"✅ <b>{tool_name}</b> — auto-approved ({reason})"
        else:
            lines = [f"✅ Auto-approved {len(items)} tools:"]
            for tool_name, _reason in items:
                lines.append(f"  • {tool_name}")
            lines.append(f"<i>({items[0][1]})</i>")
            text = "\n".join(lines)

        try:
            await self._app.bot.send_message(
                chat_id=self._forum_group_id,
                message_thread_id=topic_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception:
            pass

    def _stop_typing(self, session_id: str) -> None:
        """Cancel the background typing loop for a session."""
        task = self._typing_tasks.pop(session_id, None)
        if task:
            task.cancel()

    async def _typing_loop(self, session_id: str, topic_id: int) -> None:
        """Send typing indicator every 5s until cancelled."""
        try:
            while True:
                try:
                    await self._app.bot.send_chat_action(
                        chat_id=self._forum_group_id,
                        message_thread_id=topic_id,
                        action="typing",
                    )
                except Exception:
                    logger.debug("Failed to send typing action", session_id=session_id)
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    async def on_typing(self, session_id: str) -> None:
        """Start a repeating typing indicator for the session."""
        if not self._app:
            return

        topic_id = self._state.get_topic_for_session(session_id)
        if not topic_id:
            return

        # Already running for this session
        if session_id in self._typing_tasks:
            return

        self._typing_tasks[session_id] = asyncio.create_task(
            self._typing_loop(session_id, topic_id)
        )

    async def on_approval_request(
        self, session_id: str, request: ApprovalRequest
    ) -> None:
        """Send an approval request with inline keyboard buttons.

        Supports:
        - permission requests (Allow/Deny + timers)
        - choice requests (arbitrary options; sends selected option as session input)
        """
        self._stop_typing(session_id)
        if not self._app:
            logger.warning("Telegram app not initialized")
            return

        # Choice requests: present options directly (not allow/deny).
        if request.kind == "choice":
            topic_id = self._state.get_topic_for_session(session_id)
            if not topic_id:
                logger.warning("No Telegram topic for session", session_id=session_id)
                return

            try:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            except ImportError:
                logger.error("python-telegram-bot not installed")
                return

            self.set_pending_permission(session_id, request)

            md = f"⚠️ *{request.title}*\n\n{request.description}"
            html_text = markdown_to_telegram_html(md)
            rid = request.request_id
            self._approval_html[rid] = html_text

            rows: list[list[InlineKeyboardButton]] = []
            current: list[InlineKeyboardButton] = []
            for idx, label in enumerate(request.options, start=1):
                current.append(
                    InlineKeyboardButton(
                        f"{idx}. {label}",
                        callback_data=f"approval:{rid}:Choose:{idx}",
                    )
                )
                if len(current) == 2:
                    rows.append(current)
                    current = []
            if current:
                rows.append(current)

            reply_markup = InlineKeyboardMarkup(rows)
            try:
                await self._app.bot.send_message(
                    chat_id=self._forum_group_id,
                    message_thread_id=topic_id,
                    text=html_text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception(
                    "Failed to send choice request",
                    session_id=session_id,
                    request_id=request.request_id,
                )
            return

        reason: str | None = None
        if request.kind == "permission":
            reason = self.check_auto_approve(session_id, request.title)
        if reason:
            await self._auto_approve(session_id, request, reason=reason)
            self.buffer_auto_approve_notification(session_id, request.title, reason)
            return

        topic_id = self._state.get_topic_for_session(session_id)
        if not topic_id:
            logger.warning("No Telegram topic for session", session_id=session_id)
            return

        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except ImportError:
            logger.error("python-telegram-bot not installed")
            return

        description, was_truncated = self._format_tool_input_html(request.description)

        tool_name = request.title
        rid = request.request_id
        tool_lower = tool_name.strip().lower()
        is_task = any(tool_lower.startswith(p) for p in self._NEVER_AUTO_APPROVE)

        # Cache full description for "Show All"
        if was_truncated:
            self._pending_descriptions[rid] = (tool_name, request.description)

        self.set_pending_permission(session_id, request)

        # "Task" prompts are effectively "proceed/cancel". Keep callback values as Allow/Deny.
        row_actions = [
            InlineKeyboardButton(
                "Proceed" if is_task else "Allow",
                callback_data=f"approval:{rid}:Allow",
            ),
            InlineKeyboardButton(
                "Cancel" if is_task else "Deny",
                callback_data=f"approval:{rid}:Deny",
            ),
            InlineKeyboardButton(
                "Cancel ✏️" if is_task else "Deny ✏️",
                callback_data=f"approval:{rid}:DenyWithReason",
            ),
        ]

        rows = [row_actions]
        if not is_task:
            row_timers = [
                InlineKeyboardButton(
                    f"Allow {tool_name} (30m)",
                    callback_data=f"approval:{rid}:AllowTool:{tool_name}",
                ),
                InlineKeyboardButton(
                    "Allow All (30m)", callback_data=f"approval:{rid}:AllowAll"
                ),
            ]
            rows.append(row_timers)
            # Directory-scoped timer button
            from tether.store import store as _store

            _sess = _store.get_session(session_id)
            if _sess and _sess.directory:
                dir_short = _sess.directory.rstrip("/").rsplit("/", 1)[-1] or "dir"
                rows.append(
                    [
                        InlineKeyboardButton(
                            f"Allow {dir_short}/ (30m)",
                            callback_data=f"approval:{rid}:AllowDir",
                        )
                    ]
                )
        if was_truncated:
            rows.append(
                [
                    InlineKeyboardButton(
                        "Show All", callback_data=f"approval:{rid}:ShowAll"
                    )
                ]
            )

        reply_markup = InlineKeyboardMarkup(rows)
        text = f"⚠️ <b>{tool_name}</b>\n\n{description}"
        self._approval_html[rid] = text

        try:
            await self._app.bot.send_message(
                chat_id=self._forum_group_id,
                message_thread_id=topic_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        except Exception:
            logger.exception(
                "Failed to send approval request",
                session_id=session_id,
                request_id=request.request_id,
            )

    async def on_session_removed(self, session_id: str) -> None:
        """Clean up state when a session is deleted."""
        self._stop_typing(session_id)
        await super().on_session_removed(session_id)
        self._state.remove_session(session_id)
        logger.info("Cleaned up Telegram state for session", session_id=session_id)

    async def on_status_change(
        self, session_id: str, status: str, metadata: dict | None = None
    ) -> None:
        """Send status change notification to Telegram."""
        if not self._app:
            return

        if status == "error" and not self._should_send_error_status(session_id):
            return

        topic_id = self._state.get_topic_for_session(session_id)
        if not topic_id:
            return

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
        """Create a Telegram forum topic for a session."""
        if not self._app:
            raise RuntimeError("Telegram app not initialized")

        try:
            topic = await self._app.bot.create_forum_topic(
                chat_id=self._forum_group_id,
                name=session_name[:128],  # Telegram limit
                icon_color=7322096,  # Light blue
            )

            topic_id = topic.message_thread_id
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
