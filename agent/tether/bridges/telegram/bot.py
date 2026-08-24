"""Tether-local Telegram bridge wrapper with richer output formatting."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import html
import re
from typing import Any
import uuid

import structlog
from agent_tether.telegram.bot import TelegramBridge as UpstreamTelegramBridge

try:
    from telegram import BotCommand
    from telegram.error import BadRequest
    from telegram.ext import ApplicationHandlerStop
except ImportError:
    BadRequest = None
    BotCommand = None

    class ApplicationHandlerStop(Exception):
        """Fallback used when python-telegram-bot is not installed."""


from tether.bridges.attachments import attachments_from_metadata
from tether.bridges.base import (
    ApprovalRequest,
    _EXTERNAL_MAX_FETCH,
    _EXTERNAL_PAGE_SIZE,
    _relative_time,
)
from tether.bridges.command_catalog import help_text, telegram_menu_commands
from tether.bridges.compact_api import compact_session
from tether.bridges.dedupe import (
    ShortLivedMessageDedupe,
    is_obvious_telegram_bot_loop,
    telegram_update_key,
)
from tether.bridges.image_io import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_MESSAGE,
    make_bridge_image,
)
from tether.bridges.media_io import (
    MAX_MEDIA_BYTES,
    BridgeMediaFile,
    append_media_file_references,
    download_with_media_policy,
    store_bridge_media_file,
    supported_media_type,
)
from tether.bridges.model_api import (
    format_model_info,
    get_session_model,
    set_session_model,
)
from tether.bridges.output_policy_api import (
    format_bridge_output_policy,
    get_bridge_output_policy,
    parse_buffer_arg,
    parse_verbosity_arg,
    set_bridge_output_policy,
)
from tether.bridges.rich_output import render_telegram_messages
from tether.bridges.retry import bridge_retry_after_s, with_bridge_send_retry
from tether.bridges.telegram.formatting import markdown_to_telegram_html
from tether.settings import settings

logger = structlog.get_logger(__name__)
_TELEGRAM_MEDIA_GROUP_DEBOUNCE_S = 0.7
_TELEGRAM_OUTPUT_MIN_INTERVAL_S = 1.25
_TELEGRAM_RATE_LIMIT_FALLBACK_PAUSE_S = 30.0
_TELEGRAM_OUTPUT_SEND_ATTEMPTS = 20


@dataclass
class _TelegramMediaGroupBuffer:
    """Pending Telegram album media before dispatching as one turn."""

    session_id: str
    topic_id: int
    message: Any
    texts: list[str] = field(default_factory=list)
    images: list[dict[str, str]] = field(default_factory=list)
    skipped_count: int = 0
    total_count: int = 0


class TelegramBridge(UpstreamTelegramBridge):
    """Render tool calls and pass Telegram images through to sessions."""

    @staticmethod
    def _agent_to_adapter(raw: str) -> str | None:
        """Map user-friendly agent names to local adapter names."""

        normalized = (raw or "").strip().lower()
        if normalized in {"automation", "script"}:
            return "automation"
        if normalized in {"pi", "pi_rpc"}:
            return "pi_rpc"
        return UpstreamTelegramBridge._agent_to_adapter(raw)

    @staticmethod
    def _adapter_label(adapter: str | None) -> str | None:
        """Map local adapter names to user-friendly labels."""

        if adapter == "automation":
            return "Automation"
        return UpstreamTelegramBridge._adapter_label(adapter)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._media_group_buffers: dict[str, _TelegramMediaGroupBuffer] = {}
        self._media_group_tasks: dict[str, asyncio.Task] = {}
        self._message_dedupe = ShortLivedMessageDedupe()
        self._output_send_lock = asyncio.Lock()
        self._allowed_user_ids = settings.telegram_allowed_user_ids()
        self._output_paused_until = 0.0
        self._last_output_send_at = 0.0

    @staticmethod
    def _update_user(update: Any) -> Any:
        """Return the Telegram user for an update, if present."""
        user = getattr(update, "effective_user", None)
        if user is None and getattr(update, "callback_query", None):
            user = getattr(update.callback_query, "from_user", None)
        if user is None and getattr(update, "message", None):
            user = getattr(update.message, "from_user", None)
        return user

    @classmethod
    def _update_user_id(cls, update: Any) -> int | None:
        """Return the Telegram user ID for an update, if present."""
        user_id = getattr(cls._update_user(update), "id", None)
        try:
            return int(user_id) if user_id is not None else None
        except (TypeError, ValueError):
            return None

    def _is_authorized_update(self, update: Any) -> bool:
        """Return whether a Telegram update may control Tether."""
        if not self._allowed_user_ids:
            return True
        user_id = self._update_user_id(update)
        return user_id in self._allowed_user_ids

    async def _guard_update(self, update: Any, context: Any) -> None:
        """Stop unauthorized Telegram users before command handlers run."""
        if bool(getattr(self._update_user(update), "is_bot", False)):
            # ASVS 8.3.3: never treat an intermediary bot as the human originator.
            raise ApplicationHandlerStop
        if self._is_authorized_update(update):
            return

        user_id = self._update_user_id(update)
        logger.warning("Blocked unauthorized Telegram update", user_id=user_id)
        if getattr(update, "callback_query", None):
            await update.callback_query.answer(
                "This Tether bridge is restricted.", show_alert=True
            )
        elif getattr(update, "message", None):
            await update.message.reply_text("🔒 This Tether bridge is restricted.")
        raise ApplicationHandlerStop

    def _compact_callback_token(self, request_id: str) -> str:
        """Create a short Telegram callback token for long request IDs."""

        tokens = getattr(self, "_compact_approval_tokens", None)
        if tokens is None:
            tokens = {}
            self._compact_approval_tokens = tokens
        token = uuid.uuid4().hex[:10]
        tokens[token] = request_id
        return token

    def _pop_compact_callback_token(self, token: str) -> str | None:
        """Resolve and remove a compact Telegram callback token."""

        tokens = getattr(self, "_compact_approval_tokens", None)
        if not tokens:
            return None
        return tokens.pop(token, None)

    async def on_approval_request(
        self, session_id: str, request: ApprovalRequest
    ) -> None:
        """Send approval requests, keeping Telegram callback payloads short."""

        if request.kind != "choice":
            await super().on_approval_request(session_id, request)
            return

        self._stop_typing(session_id)
        if not self._app:
            logger.warning("Telegram app not initialized")
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

        self.set_pending_permission(session_id, request)

        md = f"⚠️ *{request.title}*\n\n{request.description}"
        html_text = markdown_to_telegram_html(md)
        token = self._compact_callback_token(request.request_id)
        self._approval_html[request.request_id] = html_text

        rows: list[list[InlineKeyboardButton]] = []
        current: list[InlineKeyboardButton] = []
        for index, label in enumerate(request.options, start=1):
            current.append(
                InlineKeyboardButton(
                    f"{index}. {label}",
                    callback_data=f"choice:{token}:{index}",
                )
            )
            if len(current) == 2:
                rows.append(current)
                current = []
        if current:
            rows.append(current)

        try:
            await self._app.bot.send_message(
                chat_id=self._forum_group_id,
                message_thread_id=topic_id,
                text=html_text,
                reply_markup=InlineKeyboardMarkup(rows),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception(
                "Failed to send choice request",
                session_id=session_id,
                request_id=request.request_id,
            )

    async def _handle_callback_query(self, update: Any, context: Any) -> None:
        """Handle compact local callbacks before falling back to upstream."""

        query = getattr(update, "callback_query", None)
        data = getattr(query, "data", "") if query else ""
        if not data.startswith("choice:"):
            await super()._handle_callback_query(update, context)
            return

        await query.answer()
        try:
            _, token, raw_index = data.split(":", 2)
            option_index = int(raw_index) - 1
        except Exception:
            logger.warning("Invalid compact choice callback data", data=data)
            return

        request_id = self._pop_compact_callback_token(token)
        if not request_id:
            await query.edit_message_text(text="❌ Request expired.")
            return

        topic_id = getattr(query.message, "message_thread_id", None)
        if not topic_id:
            logger.warning("Callback from message with no topic ID")
            return

        session_id = self._state.get_session_for_topic(topic_id)
        if not session_id:
            logger.warning("No session for topic", topic_id=topic_id)
            await query.edit_message_text(text="❌ Error: Session not found")
            return

        original_html = self._approval_html.get(request_id, query.message.text)
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

        if option_index < 0 or option_index >= len(pending_req.options):
            await query.answer("Invalid option")
            return

        selected = pending_req.options[option_index]
        username = self._display_name(query.from_user)
        if request_id.startswith("pi_extui:"):
            ok = await self._respond_to_permission(
                session_id,
                request_id,
                allow=True,
                message=selected,
            )
            if not ok:
                await query.edit_message_text(
                    text=f"{original_html}\n\n❌ Error: Failed to submit response",
                    parse_mode="HTML",
                )
                return
        else:
            await self._send_input_or_start_via_api(
                session_id=session_id, text=selected
            )
            self.clear_pending_permission(session_id)
        self._approval_html.pop(request_id, None)
        await query.edit_message_text(
            text=f"{original_html}\n\n✅ {selected} by {username}",
            parse_mode="HTML",
        )

    async def rename_thread(self, session_id: str, session_name: str) -> str:
        """Rename a Telegram forum topic for a bound session."""
        if not self._app:
            raise RuntimeError("Telegram app not initialized")

        topic_id = self._state.get_topic_for_session(session_id)
        if not topic_id:
            raise RuntimeError(f"No Telegram topic for session {session_id}")

        topic_name = session_name[:128]
        try:
            await self._app.bot.edit_forum_topic(
                chat_id=self._forum_group_id,
                message_thread_id=topic_id,
                name=topic_name,
            )
            self._state.set_topic_for_session(session_id, topic_id, topic_name)
            logger.info(
                "Renamed Telegram topic",
                session_id=session_id,
                topic_id=topic_id,
                name=topic_name,
            )
            return topic_name
        except Exception as exc:
            logger.exception(
                "Failed to rename Telegram topic",
                session_id=session_id,
                topic_id=topic_id,
                name=topic_name,
            )
            raise RuntimeError(f"Failed to rename Telegram topic: {exc}") from exc

    async def start(self) -> None:
        """Start Telegram and register a media handler for session topics."""

        await super().start()
        if not self._app:
            return

        try:
            from telegram.ext import (
                CallbackQueryHandler,
                CommandHandler,
                MessageHandler,
                filters,
            )
        except ImportError:
            return

        if not self._allowed_user_ids:
            logger.warning(
                "Telegram bridge has no sender allowlist; every user in the forum can control bound sessions",
                env_var="TELEGRAM_ALLOWED_USER_IDS",
            )
        self._app.add_handler(MessageHandler(filters.ALL, self._guard_update), group=-1)
        self._app.add_handler(CallbackQueryHandler(self._guard_update), group=-1)
        self._app.add_handler(
            CallbackQueryHandler(self._handle_callback_query, pattern=r"^choice:")
        )

        self._app.add_handler(CommandHandler("sync", self._cmd_sync))
        self._app.add_handler(CommandHandler("compact", self._cmd_compact))
        self._app.add_handler(CommandHandler("models", self._cmd_models))
        self._app.add_handler(CommandHandler("model", self._cmd_model))
        self._app.add_handler(CommandHandler("verbosity", self._cmd_verbosity))
        self._app.add_handler(CommandHandler("buffer", self._cmd_buffer))
        await self._register_command_menu()
        self._app.add_handler(
            MessageHandler(
                (filters.PHOTO | filters.ATTACHMENT) & filters.ChatType.SUPERGROUP,
                self._handle_media_message,
            )
        )

    @staticmethod
    def _clip_mono(text: str, width: int) -> str:
        """Clip text for a fixed-width Telegram listing."""
        text = " ".join(str(text or "").split())
        if len(text) <= width:
            return text
        return text[: max(1, width - 1)].rstrip() + "…"

    def _format_external_page_html(self, page: int) -> tuple[str, int, int]:
        """Format external sessions for Telegram with aligned monospace rows."""
        sessions = self._external_view or []
        if not sessions:
            if self._external_query:
                query = html.escape(self._external_query)
                return (
                    f"No external sessions match directory search: <code>{query}</code>\n\n"
                    "Try a different query, or run /list to clear the search.",
                    1,
                    1,
                )
            return (
                "No external sessions found.\n\n"
                "Start a Claude Code or Codex session first, then use /list to see it.",
                1,
                1,
            )

        total = len(sessions)
        total_pages = max(1, (total + _EXTERNAL_PAGE_SIZE - 1) // _EXTERNAL_PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * _EXTERNAL_PAGE_SIZE
        end = min(start + _EXTERNAL_PAGE_SIZE, total)

        title = f"External Sessions (page {page}/{total_pages})"
        if self._external_query:
            title += f" [search: {self._external_query}]"

        rows: list[str] = []
        for idx in range(start, end):
            session = sessions[idx]
            number = idx + 1
            directory = str(session.get("directory") or "")
            dir_short = directory.rsplit("/", 1)[-1] if directory else "?"
            runner = str(session.get("runner_type") or "?")
            age = _relative_time(str(session.get("last_activity") or ""))
            prompt = str(
                session.get("last_prompt") or session.get("first_prompt") or ""
            )
            header = f"{number:>2}. {self._clip_mono(dir_short, 28):<28} ({runner})"
            if age:
                header += f" • {age}"
            rows.append(header.rstrip())
            if prompt:
                rows.append(f"    ↳ {self._clip_mono(prompt, 58)}")

        lines = [
            html.escape(title) + ":",
            "<pre>" + html.escape("\n".join(rows)) + "</pre>",
        ]
        if (
            not self._external_query
            and len(self._cached_external) == _EXTERNAL_MAX_FETCH
        ):
            lines.append(f"Showing up to {_EXTERNAL_MAX_FETCH} sessions (API limit).")
        lines.append("/attach &lt;number&gt; to attach.")
        return "\n\n".join(lines), page, total_pages

    async def _cmd_list(self, update: Any, context: Any) -> None:
        """Handle /list with Telegram-friendly aligned formatting."""
        page = 1
        query: str | None = None
        args = getattr(context, "args", None) or []
        user_id = getattr(getattr(update, "effective_user", None), "id", None)
        if args:
            first = args[0]
            try:
                page = int(first)
                query = self._external_query
            except Exception:
                query = " ".join(args).strip()
                page = 1

        try:
            await self._refresh_external_cache()
            if not args:
                self._set_external_view(None)
            else:
                self._set_external_view(query)
        except Exception:
            logger.exception("Failed to fetch external sessions")
            await update.message.reply_text("Failed to list external sessions.")
            return

        if user_id is not None:
            self._external_view_by_user[int(user_id)] = list(self._external_view)

        text, page, total_pages = self._format_external_page_html(page)
        reply_markup = self._external_pagination_markup(page, total_pages)
        await update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )

    async def _handle_list_callback_query(self, update: Any, context: Any) -> None:
        """Handle Telegram pagination with aligned HTML formatting."""
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
                with contextlib.suppress(Exception):
                    await query.edit_message_text(
                        "Failed to refresh external sessions."
                    )
                return
            self._set_external_view(self._external_query)
            page = 1
        else:
            try:
                _, kind, value = data.split(":", 2)
                if kind != "page":
                    return
                page = int(value)
            except Exception:
                return

        if not self._cached_external:
            try:
                await self._refresh_external_cache()
            except Exception:
                logger.exception("Failed to fetch external sessions for pagination")
                with contextlib.suppress(Exception):
                    await query.edit_message_text(
                        "Failed to list external sessions. Run /list again."
                    )
                return
            self._set_external_view(self._external_query)

        text, page, total_pages = self._format_external_page_html(page)
        reply_markup = self._external_pagination_markup(page, total_pages)
        try:
            await query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        except Exception:
            try:
                await query.message.reply_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("Failed to send external pagination message")

    async def _register_command_menu(self) -> None:
        """Register the local Telegram command menu."""
        if not self._app:
            return
        if BotCommand is None:
            return

        await self._app.bot.set_my_commands(
            [
                BotCommand(command, description)
                for command, description in telegram_menu_commands()
            ]
        )

    def _session_id_for_topic_message(self, message: Any) -> str | None:
        """Return the session linked to a Telegram topic message."""
        topic_id = getattr(message, "message_thread_id", None)
        if not topic_id:
            return None
        return self._state.get_session_for_topic(topic_id)

    async def _cmd_sync(self, update: Any, context: Any) -> None:
        """Handle /sync in a session topic."""
        message = getattr(update, "message", None)
        if message is None:
            return
        session_id = self._session_id_for_topic_message(message)
        if not getattr(message, "message_thread_id", None):
            await message.reply_text("Use this command inside a session topic.")
            return
        if not session_id:
            await message.reply_text("No session linked to this topic.")
            return
        if not self._callbacks.sync_session:
            await message.reply_text("Sync is not supported by this Tether version.")
            return

        try:
            result = await self._callbacks.sync_session(session_id)
            synced = result.get("synced", 0)
            total = result.get("total", 0)
            if synced:
                await message.reply_text(
                    f"🔄 Synced {synced} new message(s) ({total} total)."
                )
            else:
                await message.reply_text(
                    f"✅ Already up to date ({total} message(s) total)."
                )
        except Exception as exc:
            logger.exception("Failed to sync Telegram session", session_id=session_id)
            await message.reply_text(f"Failed to sync: {exc}")

    async def _cmd_compact(self, update: Any, context: Any) -> None:
        """Handle /compact in a session topic."""
        message = getattr(update, "message", None)
        if message is None:
            return
        session_id = self._session_id_for_topic_message(message)
        if not getattr(message, "message_thread_id", None):
            await message.reply_text("Use this command inside a session topic.")
            return
        if not session_id:
            await message.reply_text("No session linked to this topic.")
            return
        custom_instructions = " ".join(getattr(context, "args", []) or []).strip()
        try:
            await compact_session(session_id, custom_instructions or None)
            await message.reply_text("🧹 Compaction requested.")
        except Exception as exc:
            logger.exception(
                "Failed to compact Telegram session", session_id=session_id
            )
            await message.reply_text(f"Failed to compact session: {exc}")

    async def _cmd_models(self, update: Any, context: Any) -> None:
        """Handle /models in a session topic."""
        message = getattr(update, "message", None)
        if message is None:
            return
        session_id = self._session_id_for_topic_message(message)
        if not getattr(message, "message_thread_id", None):
            await message.reply_text("Use this command inside a session topic.")
            return
        if not session_id:
            await message.reply_text("No session linked to this topic.")
            return
        try:
            await message.reply_text(
                format_model_info(await get_session_model(session_id))
            )
        except Exception as exc:
            logger.exception(
                "Failed to fetch Telegram session model", session_id=session_id
            )
            await message.reply_text(f"Failed to fetch model: {exc}")

    async def _cmd_model(self, update: Any, context: Any) -> None:
        """Handle /model in a session topic."""
        message = getattr(update, "message", None)
        if message is None:
            return
        session_id = self._session_id_for_topic_message(message)
        if not getattr(message, "message_thread_id", None):
            await message.reply_text("Use this command inside a session topic.")
            return
        if not session_id:
            await message.reply_text("No session linked to this topic.")
            return
        model = " ".join(getattr(context, "args", []) or []).strip()
        try:
            if model:
                session = await set_session_model(session_id, model)
                await message.reply_text(
                    f"✅ Model set to {session.get('model') or model}."
                )
            else:
                await message.reply_text(
                    format_model_info(await get_session_model(session_id))
                )
        except Exception as exc:
            logger.exception(
                "Failed to update Telegram session model", session_id=session_id
            )
            await message.reply_text(f"Failed to update model: {exc}")

    async def _cmd_verbosity(self, update: Any, context: Any) -> None:
        """Handle /verbosity in a session topic."""
        message = getattr(update, "message", None)
        if message is None:
            return
        session_id = self._session_id_for_topic_message(message)
        if not getattr(message, "message_thread_id", None):
            await message.reply_text("Use this command inside a session topic.")
            return
        if not session_id:
            await message.reply_text("No session linked to this topic.")
            return
        raw = " ".join(getattr(context, "args", []) or [])
        verbosity, error, clear = parse_verbosity_arg(raw)
        if error:
            await message.reply_text(error)
            return
        try:
            if verbosity or clear:
                session = await set_bridge_output_policy(
                    session_id, verbosity=verbosity, clear_verbosity=clear
                )
            else:
                session = await get_bridge_output_policy(session_id)
            await message.reply_text(format_bridge_output_policy(session))
        except Exception as exc:
            logger.exception(
                "Failed to update Telegram output verbosity", session_id=session_id
            )
            await message.reply_text(f"Failed to update verbosity: {exc}")

    async def _cmd_buffer(self, update: Any, context: Any) -> None:
        """Handle /buffer in a session topic."""
        message = getattr(update, "message", None)
        if message is None:
            return
        session_id = self._session_id_for_topic_message(message)
        if not getattr(message, "message_thread_id", None):
            await message.reply_text("Use this command inside a session topic.")
            return
        if not session_id:
            await message.reply_text("No session linked to this topic.")
            return
        raw = " ".join(getattr(context, "args", []) or [])
        seconds, error, clear = parse_buffer_arg(raw)
        if error:
            await message.reply_text(error)
            return
        try:
            if seconds is not None or clear:
                session = await set_bridge_output_policy(
                    session_id, buffer_max_seconds=seconds, clear_buffer=clear
                )
            else:
                session = await get_bridge_output_policy(session_id)
            await message.reply_text(format_bridge_output_policy(session))
        except Exception as exc:
            logger.exception(
                "Failed to update Telegram output buffer", session_id=session_id
            )
            await message.reply_text(f"Failed to update buffer: {exc}")

    async def _cmd_help(self, update: Any, context: Any) -> None:
        """Handle /help."""
        await update.message.reply_text(help_text("telegram", prefix="/"))

    async def _collect_message_media(
        self,
        update: object,
        session_id: str,
        *,
        collect_files: bool = True,
    ) -> tuple[list[dict[str, str]], list[BridgeMediaFile]]:
        """Download and validate supported Telegram attachments."""

        message = getattr(update, "message", None)
        if message is None:
            return [], []

        photos = list(getattr(message, "photo", []) or [])
        document = getattr(message, "document", None)
        audio = getattr(message, "audio", None)
        video = getattr(message, "video", None)
        image_ref = photos[-1] if photos else document
        if image_ref is not None:
            declared_mime_type = (
                getattr(document, "mime_type", None) if document else "image/jpeg"
            )
            if photos or str(declared_mime_type or "").lower().startswith("image/"):
                filename = getattr(document, "file_name", None) if document else None
                size = int(getattr(image_ref, "file_size", 0) or 0)
                if size > MAX_IMAGE_BYTES:
                    await message.reply_text(
                        f"⚠️ Skipped image: image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
                    )
                    return [], []
                try:
                    telegram_file = await image_ref.get_file()
                    data = await download_with_media_policy(
                        telegram_file.download_as_bytearray,
                        platform="telegram",
                        url=getattr(telegram_file, "file_path", None),
                    )
                    image = make_bridge_image(
                        data,
                        declared_mime_type=declared_mime_type,
                        filename=filename,
                    )
                except ValueError as exc:
                    await message.reply_text(f"⚠️ Skipped image: {exc}")
                    return [], []
                except Exception:
                    logger.exception("Failed to read Telegram image attachment")
                    await message.reply_text("⚠️ Failed to read an image attachment.")
                    return [], []
                return [image.as_api_payload()], []

        if not collect_files:
            return [], []

        media_ref = document or audio or video
        if media_ref is None:
            return [], []
        mime_type = getattr(media_ref, "mime_type", None)
        if not supported_media_type(mime_type):
            return [], []
        size = int(getattr(media_ref, "file_size", 0) or 0)
        if size > MAX_MEDIA_BYTES:
            await message.reply_text(
                f"⚠️ Skipped attachment: file is larger than {MAX_MEDIA_BYTES // (1024 * 1024)} MB"
            )
            return [], []
        try:
            telegram_file = await media_ref.get_file()
            data = await download_with_media_policy(
                telegram_file.download_as_bytearray,
                platform="telegram",
                url=getattr(telegram_file, "file_path", None),
            )
            media_file = store_bridge_media_file(
                session_id=session_id,
                data=data,
                filename=getattr(media_ref, "file_name", None),
                mime_type=mime_type,
            )
        except ValueError as exc:
            await message.reply_text(f"⚠️ Skipped attachment: {exc}")
            return [], []
        except Exception:
            logger.exception("Failed to read Telegram media attachment")
            await message.reply_text("⚠️ Failed to read an attachment.")
            return [], []
        return [], [media_file]

    async def _collect_message_images(self, update: object) -> list[dict[str, str]]:
        """Download and validate supported Telegram image attachments."""

        images, _ = await self._collect_message_media(
            update, "unknown", collect_files=False
        )
        return images

    async def _handle_media_message(self, update: object, context: object) -> None:
        """Handle Telegram photos and forward them as native image input."""

        message = getattr(update, "message", None)
        if message is None:
            return
        if self._should_ignore_inbound_media(update):
            return

        topic_id = getattr(message, "message_thread_id", None)
        if not topic_id:
            await message.reply_text(
                "💡 Send attachments in a session topic to interact with that agent."
            )
            return

        session_id = self._state.get_session_for_topic(topic_id)
        if not session_id:
            await message.reply_text("⚠️ No active session is linked to this topic.")
            return

        group_id = str(getattr(message, "media_group_id", "") or "").strip()
        if group_id:
            await self._buffer_media_group(update, session_id, topic_id, group_id)
            return

        text = (getattr(message, "caption", None) or "").strip()
        images, files = await self._collect_message_media(update, session_id)
        text = append_media_file_references(text, files)
        await self._send_media_input(
            session_id=session_id,
            topic_id=topic_id,
            text=text,
            images=images,
            files=files,
            message=message,
        )

    def _should_ignore_inbound_media(self, update: object) -> bool:
        """Suppress duplicate Telegram media deliveries and obvious bot loops."""

        if is_obvious_telegram_bot_loop(update):
            return True
        if self._message_dedupe.seen_recently(telegram_update_key(update)):
            message = getattr(update, "message", None)
            logger.debug(
                "Dropped duplicate Telegram inbound media",
                message_id=getattr(message, "message_id", None),
                chat_id=getattr(getattr(message, "chat", None), "id", None),
            )
            return True
        return False

    async def _send_media_input(
        self,
        *,
        session_id: str,
        topic_id: int,
        text: str,
        images: list[dict[str, str]],
        files: list[BridgeMediaFile],
        message: Any,
    ) -> None:
        """Forward collected media input to a session."""

        if not images and not files:
            return
        if not text:
            text = "Please look at this attachment."

        try:
            await self.on_typing(session_id)
            if images:
                await self._callbacks.send_input(session_id, text, images=images)
            else:
                await self._callbacks.send_input(session_id, text)
            logger.info(
                "Forwarded media input from Telegram",
                session_id=session_id,
                topic_id=topic_id,
                image_count=len(images),
                file_count=len(files),
            )
        except Exception as exc:
            logger.exception(
                "Failed to forward Telegram media input",
                session_id=session_id,
                topic_id=topic_id,
            )
            await self.on_typing_stopped(session_id)
            await message.reply_text(f"❌ Failed to send input: {exc}")

    async def _send_image_input(
        self,
        *,
        session_id: str,
        topic_id: int,
        text: str,
        images: list[dict[str, str]],
        message: Any,
    ) -> None:
        """Forward collected image input to a session."""

        await self._send_media_input(
            session_id=session_id,
            topic_id=topic_id,
            text=text,
            images=images,
            files=[],
            message=message,
        )

    async def _buffer_media_group(
        self,
        update: object,
        session_id: str,
        topic_id: int,
        group_id: str,
    ) -> None:
        """Buffer Telegram album entries and dispatch them as one turn."""

        message = getattr(update, "message", None)
        if message is None:
            return

        chat_id = getattr(getattr(message, "chat", None), "id", "")
        key = f"{chat_id}:{topic_id}:{group_id}"
        buffer = self._media_group_buffers.setdefault(
            key,
            _TelegramMediaGroupBuffer(
                session_id=session_id,
                topic_id=topic_id,
                message=message,
            ),
        )
        text = (getattr(message, "caption", None) or "").strip()
        if text:
            buffer.texts.append(text)

        buffer.total_count += 1
        if len(buffer.images) >= MAX_IMAGES_PER_MESSAGE:
            buffer.skipped_count += 1
        else:
            images = await self._collect_message_images(update)
            remaining = MAX_IMAGES_PER_MESSAGE - len(buffer.images)
            accepted = images[:remaining]
            buffer.images.extend(accepted)
            if len(accepted) < len(images) or not images:
                buffer.skipped_count += 1

        existing = self._media_group_tasks.pop(key, None)
        if existing and not existing.done():
            existing.cancel()

        async def _delayed_flush() -> None:
            try:
                await asyncio.sleep(_TELEGRAM_MEDIA_GROUP_DEBOUNCE_S)
            except asyncio.CancelledError:
                return
            self._media_group_tasks.pop(key, None)
            await self._flush_media_group(key)

        self._media_group_tasks[key] = asyncio.create_task(_delayed_flush())

    async def _flush_media_group(self, key: str) -> None:
        """Send a buffered Telegram album to the session."""

        pending_task = self._media_group_tasks.pop(key, None)
        if pending_task and pending_task is not asyncio.current_task():
            pending_task.cancel()

        buffer = self._media_group_buffers.pop(key, None)
        if buffer is None:
            return
        if buffer.skipped_count > 0:
            await self._send_media_group_skip_warning(buffer)

        text = "\n".join(buffer.texts).strip()
        await self._send_image_input(
            session_id=buffer.session_id,
            topic_id=buffer.topic_id,
            text=text,
            images=buffer.images,
            message=buffer.message,
        )

    async def _send_media_group_skip_warning(
        self,
        buffer: _TelegramMediaGroupBuffer,
    ) -> None:
        """Notify Telegram users when an album was only partially accepted."""

        total = max(buffer.total_count, len(buffer.images) + buffer.skipped_count)
        skipped = buffer.skipped_count
        was_or_were = "was" if skipped == 1 else "were"
        try:
            await buffer.message.reply_text(
                f"⚠️ Received {len(buffer.images)} of {total} images; {skipped} {was_or_were} skipped."
            )
        except Exception:
            logger.exception(
                "Failed to send Telegram media group warning",
                session_id=buffer.session_id,
                topic_id=buffer.topic_id,
            )

    def _compact_output_messages(self, messages: list[str]) -> list[str]:
        """Limit one bridge output when a cap is configured."""

        max_messages = settings.telegram_output_max_messages()
        if max_messages <= 0 or len(messages) <= max_messages:
            return messages

        if max_messages == 1:
            return [messages[-1]]

        notice_slots = 1
        head_count = max(0, max_messages - notice_slots - 1)
        omitted = len(messages) - head_count - 1
        notice = (
            "⚠️ <b>Telegram output shortened</b>\n"
            f"Skipped {omitted} middle message chunks to avoid flood limits. "
            "The final chunk is still shown below. The full output remains in Tether and the local session log."
        )
        return [*messages[:head_count], notice, messages[-1]]

    @staticmethod
    def _is_missing_thread_error(exc: Exception) -> bool:
        """Return true when Telegram reports a deleted forum topic."""
        if BadRequest is not None and isinstance(exc, BadRequest):
            return "message thread not found" in str(exc).casefold()
        return "message thread not found" in str(exc).casefold()

    @staticmethod
    def _is_html_parse_error(exc: Exception) -> bool:
        """Return true when Telegram rejected generated HTML markup."""
        return "can't parse entities" in str(exc).casefold()

    def _drop_missing_topic_binding(self, session_id: str, topic_id: int) -> None:
        """Forget a Telegram binding after its forum topic was deleted."""
        self._state.remove_session(session_id)
        try:
            from tether.external_session_watcher import external_session_watcher
            from tether.store import store

            session = store.get_session(session_id)
            if session and session.platform == "telegram":
                session.platform = None
                session.platform_thread_id = None
                store.update_session(session)
            external_session_watcher.unregister(session_id)
        except Exception:
            logger.exception(
                "Failed to clear deleted Telegram topic binding",
                session_id=session_id,
                topic_id=topic_id,
            )

    async def _send_output_message(
        self,
        session_id: str,
        topic_id: int,
        message: str,
        *,
        parse_mode: str | None = "HTML",
    ) -> bool:
        """Send one Telegram output message with flood-limit recovery."""

        loop = asyncio.get_running_loop()
        for attempt in range(1, _TELEGRAM_OUTPUT_SEND_ATTEMPTS + 1):
            now = loop.time()
            if now < self._output_paused_until:
                pause_s = self._output_paused_until - now
                logger.warning(
                    "Telegram flood control active; delaying output",
                    session_id=session_id,
                    topic_id=topic_id,
                    pause_s=round(pause_s, 2),
                    attempt=attempt,
                )
                await asyncio.sleep(pause_s)

            delay_s = _TELEGRAM_OUTPUT_MIN_INTERVAL_S - (
                loop.time() - self._last_output_send_at
            )
            if self._last_output_send_at and delay_s > 0:
                await asyncio.sleep(delay_s)

            try:
                send_kwargs: dict[str, Any] = {
                    "chat_id": self._forum_group_id,
                    "message_thread_id": topic_id,
                    "text": message,
                }
                if parse_mode:
                    send_kwargs["parse_mode"] = parse_mode
                await with_bridge_send_retry(
                    "telegram.output",
                    lambda: self._app.bot.send_message(**send_kwargs),
                    max_delay_s=60.0,
                )
                self._last_output_send_at = loop.time()
                return True
            except Exception as exc:
                if self._is_missing_thread_error(exc):
                    logger.warning(
                        "Telegram topic is gone; clearing session binding",
                        session_id=session_id,
                        topic_id=topic_id,
                    )
                    self._drop_missing_topic_binding(session_id, topic_id)
                    return False
                retry_after = bridge_retry_after_s(exc)
                if retry_after is None:
                    raise
                pause_s = max(retry_after, _TELEGRAM_RATE_LIMIT_FALLBACK_PAUSE_S)
                self._output_paused_until = loop.time() + pause_s
                logger.warning(
                    "Telegram flood control detected; retrying output later",
                    session_id=session_id,
                    topic_id=topic_id,
                    pause_s=round(pause_s, 2),
                    attempt=attempt,
                )

        raise RuntimeError("Telegram flood control did not recover in time")

    async def on_status_change(
        self, session_id: str, status: str, metadata: dict | None = None
    ) -> None:
        """Send useful Telegram errors while retaining upstream status handling."""
        message = str((metadata or {}).get("message") or "").replace("\x00", "").strip()
        if status != "error" or not message:
            await super().on_status_change(session_id, status, metadata=metadata)
            return
        if not self._app or not self._state.get_topic_for_session(session_id):
            return
        if not self._should_send_error_status(session_id):
            return

        # ASVS 16.5.1: show a bounded provider message, never attached diagnostics or stacks.
        await self.on_output(session_id, f"❌ Error: {message[:3500]}")

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

        messages = self._compact_output_messages(
            render_telegram_messages(text, metadata=metadata) or [text]
        )
        sent_any = False
        async with self._output_send_lock:
            for message in messages:
                try:
                    sent = await self._send_output_message(
                        session_id, topic_id, message
                    )
                    sent_any = sent_any or sent
                    if not sent:
                        break
                except Exception as exc:
                    if not self._is_html_parse_error(exc):
                        logger.exception(
                            "Failed to send Telegram message",
                            session_id=session_id,
                            topic_id=topic_id,
                        )
                        break
                    try:
                        fallback = html.unescape(re.sub(r"<[^>]+>", "", message))[:4096]
                        sent = await self._send_output_message(
                            session_id,
                            topic_id,
                            fallback,
                            parse_mode=None,
                        )
                        sent_any = sent_any or sent
                    except Exception:
                        logger.exception(
                            "Failed to send Telegram message",
                            session_id=session_id,
                            topic_id=topic_id,
                        )
                        break

        if sent_any:
            await self._send_output_attachments(session_id, topic_id, metadata=metadata)

    async def _send_output_attachments(
        self,
        session_id: str,
        topic_id: int,
        *,
        metadata: dict | None = None,
    ) -> None:
        """Upload runner-published attachments to Telegram."""

        attachments = attachments_from_metadata(
            metadata,
            max_count=MAX_IMAGES_PER_MESSAGE,
        )
        if not attachments or not self._app:
            return

        for attachment in attachments:
            try:
                with attachment.path.open("rb") as handle:
                    if attachment.send_as_image:
                        await with_bridge_send_retry(
                            "telegram.output_photo",
                            lambda handle=handle, attachment=attachment: self._app.bot.send_photo(
                                chat_id=self._forum_group_id,
                                message_thread_id=topic_id,
                                photo=handle,
                                caption=attachment.caption,
                            ),
                        )
                    else:
                        await with_bridge_send_retry(
                            "telegram.output_document",
                            lambda handle=handle, attachment=attachment: self._app.bot.send_document(
                                chat_id=self._forum_group_id,
                                message_thread_id=topic_id,
                                document=handle,
                                filename=attachment.filename,
                                caption=attachment.caption,
                            ),
                        )
            except Exception:
                logger.exception(
                    "Failed to send Telegram output attachment",
                    session_id=session_id,
                    attachment_path=str(attachment.path),
                )
                with contextlib.suppress(Exception):
                    await with_bridge_send_retry(
                        "telegram.attachment_failure_notice",
                        lambda attachment=attachment: self._app.bot.send_message(
                            chat_id=self._forum_group_id,
                            message_thread_id=topic_id,
                            text=f"Attachment upload failed: {attachment.filename}",
                        ),
                    )
