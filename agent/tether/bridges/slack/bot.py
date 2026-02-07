"""Slack bridge implementation with command handling and session threading."""

import structlog

from tether.bridges.base import (
    ApprovalRequest,
    BridgeInterface,
    _EXTERNAL_MAX_FETCH,
    _EXTERNAL_REPLAY_LIMIT,
    _EXTERNAL_REPLAY_MAX_CHARS,
)
from tether.settings import settings

logger = structlog.get_logger(__name__)

_STATE_EMOJI = {
    "CREATED": "🆕",
    "RUNNING": "🔄",
    "AWAITING_INPUT": "📝",
    "INTERRUPTING": "⏳",
    "ERROR": "❌",
}

_SLACK_THREAD_NAME_MAX_LEN = 64


class SlackBridge(BridgeInterface):
    """Slack bridge that routes agent events to Slack threads.

    Commands (in main channel): !help, !status, !list, !attach, !stop, !usage
    Session input: messages in session threads are forwarded as input.
    """

    def __init__(self, bot_token: str, channel_id: str):
        super().__init__()
        self._bot_token = bot_token
        self._channel_id = channel_id
        self._client: any = None
        self._app: any = None
        self._thread_ts: dict[str, str] = {}  # session_id -> thread_ts

    async def start(self) -> None:
        """Initialize Slack client and socket mode."""
        try:
            from slack_sdk.web.async_client import AsyncWebClient
            from slack_bolt.async_app import AsyncApp
            from slack_bolt.adapter.socket_mode.async_handler import (
                AsyncSocketModeHandler,
            )
        except ImportError:
            logger.error(
                "slack_sdk or slack_bolt not installed. Install with: pip install slack-sdk slack-bolt"
            )
            return

        self._client = AsyncWebClient(token=self._bot_token)

        # Check if socket mode is available
        app_token = settings.slack_app_token()
        if app_token:
            try:
                self._app = AsyncApp(token=self._bot_token)

                @self._app.event("message")
                async def handle_message(event, say):
                    await self._handle_message(event)

                handler = AsyncSocketModeHandler(self._app, app_token)
                import asyncio

                asyncio.create_task(handler.start_async())

                logger.info(
                    "Slack bridge initialized with socket mode",
                    channel_id=self._channel_id,
                )
            except Exception:
                logger.exception(
                    "Failed to initialize Slack socket mode, falling back to basic mode"
                )
                logger.info(
                    "Slack bridge initialized (basic mode, no input forwarding)",
                    channel_id=self._channel_id,
                )
        else:
            logger.info(
                "Slack bridge initialized (basic mode — set SLACK_APP_TOKEN for commands and input)",
                channel_id=self._channel_id,
            )

    async def stop(self) -> None:
        """Stop Slack client."""
        if self._client:
            await self._client.close()
        logger.info("Slack bridge stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_external_thread_name(self, *, directory: str, session_id: str) -> str:
        dir_short = (directory or "").rstrip("/").rsplit("/", 1)[-1] or "session"
        raw_id = (session_id or "").strip()
        raw_id = raw_id.removeprefix("sess_")
        suffix = (raw_id[-6:] if raw_id else "") or "unknown"
        max_dir_len = max(1, _SLACK_THREAD_NAME_MAX_LEN - (1 + len(suffix)))
        if len(dir_short) > max_dir_len:
            if max_dir_len <= 3:
                dir_short = dir_short[:max_dir_len]
            else:
                dir_short = dir_short[: max_dir_len - 3] + "..."
        return f"{dir_short} {suffix}"[:_SLACK_THREAD_NAME_MAX_LEN]

    async def _send_external_session_replay(
        self, *, thread_ts: str, external_id: str, runner_type: str
    ) -> None:
        """Post recent external session history into the Slack thread."""
        if not self._client:
            return

        import httpx

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self._api_url(f"/external-sessions/{external_id}/history"),
                    headers=self._api_headers(),
                    params={
                        "runner_type": runner_type,
                        "limit": _EXTERNAL_REPLAY_LIMIT,
                    },
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

        lines: list[str] = [
            f"*Recent history* (last {min(_EXTERNAL_REPLAY_LIMIT, len(messages))} messages):\n"
        ]
        for i, msg in enumerate(messages, 1):
            role = str(msg.get("role") or "").lower()
            prefix = (
                "U"
                if role == "user"
                else ("A" if role == "assistant" else role[:1].upper() or "?")
            )
            content = (msg.get("content") or "").strip()
            thinking = (msg.get("thinking") or "").strip()
            if content and len(content) > 800:
                content = content[:800] + "..."
            if thinking and len(thinking) > 400:
                thinking = thinking[:400] + "..."
            if content:
                lines.append(f"{i}. {prefix}: {content}")
            if thinking:
                lines.append(f"   {prefix} (thinking): {thinking}")

        text = "\n".join(lines)
        if len(text) > _EXTERNAL_REPLAY_MAX_CHARS:
            text = text[: _EXTERNAL_REPLAY_MAX_CHARS - 3] + "..."

        try:
            await self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text=text,
            )
        except Exception:
            logger.exception(
                "Failed to send Slack external session replay", external_id=external_id
            )

    async def _reply(self, event: dict, text: str) -> None:
        """Send a reply to the channel/thread where the event originated."""
        if not self._client:
            return
        kwargs: dict = {"channel": event.get("channel", self._channel_id), "text": text}
        thread_ts = event.get("thread_ts") or event.get("ts")
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            await self._client.chat_postMessage(**kwargs)
        except Exception:
            logger.exception("Failed to send Slack reply")

    def _session_for_thread(self, thread_ts: str) -> str | None:
        for sid, ts in self._thread_ts.items():
            if ts == thread_ts:
                return sid
        return None

    # ------------------------------------------------------------------
    # Message router
    # ------------------------------------------------------------------

    async def _handle_message(self, event: dict) -> None:
        """Route incoming Slack messages to commands or session input."""
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return

        text = event.get("text", "").strip()
        if not text:
            return

        thread_ts = event.get("thread_ts")

        # Messages in threads → session input or thread commands
        if thread_ts:
            # Check for commands in threads
            if text.startswith("!"):
                await self._dispatch_command(event, text)
                return
            session_id = self._session_for_thread(thread_ts)
            if not session_id:
                return
            await self._forward_input(event, session_id, text)
            return

        # Top-level messages starting with ! → commands
        if text.startswith("!"):
            await self._dispatch_command(event, text)

    async def _dispatch_command(self, event: dict, text: str) -> None:
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("!help", "!start"):
            await self._cmd_help(event)
        elif cmd in ("!status", "!sessions"):
            await self._cmd_status(event)
        elif cmd == "!list":
            await self._cmd_list(event, args)
        elif cmd == "!attach":
            await self._cmd_attach(event, args)
        elif cmd == "!new":
            await self._cmd_new(event, args)
        elif cmd == "!stop":
            await self._cmd_stop(event)
        elif cmd == "!usage":
            await self._cmd_usage(event)
        else:
            await self._reply(
                event, f"Unknown command: {cmd}\nUse !help for available commands."
            )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _cmd_help(self, event: dict) -> None:
        text = (
            "Tether Commands:\n\n"
            "!status — List all sessions\n"
            "!list [page|search] — List external sessions (Claude Code, Codex)\n"
            "!attach <number> — Attach to an external session\n"
            "!new [agent] [directory] — Start a new session\n"
            "!stop — Interrupt the session in this thread\n"
            "!usage — Show token usage and cost for this session\n"
            "!help — Show this help\n\n"
            "Send a text message in a session thread to forward it as input."
        )
        await self._reply(event, text)

    @staticmethod
    def _agent_to_adapter(raw: str) -> str | None:
        key = (raw or "").strip().lower()
        if not key:
            return None
        aliases = {
            "claude": "claude_auto",
            "codex": "codex_sdk_sidecar",
        }
        if key in aliases:
            return aliases[key]
        if key in {"claude_auto", "claude_local", "claude_api", "codex_sdk_sidecar"}:
            return key
        return None

    async def _cmd_new(self, event: dict, args: str) -> None:
        """Create a new session and Slack thread.

        Usage:
        - In a session thread:
          - !new
          - !new <agent>
          - !new <directory-name>
        - In main channel:
          - !new <agent> <directory>
          - !new <directory>
        """
        parts = (args or "").split()
        thread_ts = event.get("thread_ts")

        base_session_id: str | None = None
        base_directory: str | None = None
        base_adapter: str | None = None
        if thread_ts:
            base_session_id = self._session_for_thread(thread_ts)
        if base_session_id:
            from tether.store import store

            s = store.get_session(base_session_id)
            if s:
                base_directory = s.directory
                base_adapter = s.adapter

        adapter: str | None = None
        directory_raw: str | None = None

        if not parts:
            if not base_directory:
                await self._reply(
                    event,
                    "Usage: !new <agent> <directory>\nOr, inside a session thread: !new or !new <agent>",
                )
                return
            adapter = base_adapter
            directory_raw = base_directory
        elif len(parts) == 1:
            token = parts[0]
            maybe_adapter = self._agent_to_adapter(token)
            if base_directory:
                if maybe_adapter:
                    adapter = maybe_adapter
                    directory_raw = base_directory
                else:
                    adapter = base_adapter
                    directory_raw = token
            else:
                if maybe_adapter:
                    await self._reply(event, "Usage: !new <agent> <directory>")
                    return
                directory_raw = token
        else:
            adapter = self._agent_to_adapter(parts[0])
            if not adapter:
                await self._reply(
                    event,
                    "Unknown agent. Use: claude, codex, claude_auto, claude_local, claude_api, codex_sdk_sidecar",
                )
                return
            directory_raw = " ".join(parts[1:]).strip()

        try:
            assert directory_raw is not None
            directory = await self._resolve_directory_arg(
                directory_raw, base_directory=base_directory
            )
        except Exception as e:
            await self._reply(event, f"Invalid directory: {e}")
            return

        dir_short = directory.rstrip("/").rsplit("/", 1)[-1] or "session"
        agent_label = adapter or "default"
        session_name = f"{dir_short} ({agent_label})"[:_SLACK_THREAD_NAME_MAX_LEN]

        try:
            await self._create_session_via_api(
                directory=directory,
                platform="slack",
                adapter=adapter,
                session_name=session_name,
            )
        except Exception as e:
            await self._reply(event, f"Failed to create session: {e}")
            return

        await self._reply(
            event,
            f"✅ New session created ({agent_label}) in {dir_short}. A new thread should appear in the channel.",
        )

    async def _cmd_status(self, event: dict) -> None:
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
            logger.exception("Failed to fetch sessions for !status")
            await self._reply(event, "Failed to fetch sessions.")
            return

        if not sessions:
            await self._reply(event, "No sessions.")
            return

        lines = ["Sessions:\n"]
        for s in sessions:
            emoji = _STATE_EMOJI.get(s.get("state", ""), "❓")
            name = s.get("name") or s.get("id", "")[:12]
            lines.append(f"  {emoji} {name}")
        await self._reply(event, "\n".join(lines))

    async def _cmd_list(self, event: dict, args: str) -> None:
        import httpx

        page = 1
        query: str | None = None
        if args:
            first = args.split()[0]
            try:
                page = int(first)
                query = self._external_query
            except ValueError:
                page = 1
                query = args.strip()

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self._api_url("/external-sessions"),
                    headers=self._api_headers(),
                    params={"limit": _EXTERNAL_MAX_FETCH},
                    timeout=10.0,
                )
                response.raise_for_status()
            self._cached_external = response.json()
            if not args:
                self._set_external_view(None)
            else:
                self._set_external_view(query)
        except Exception:
            logger.exception("Failed to fetch external sessions")
            await self._reply(event, "Failed to list external sessions.")
            return

        text, _, _ = self._format_external_page(page)
        await self._reply(event, text)

    async def _cmd_attach(self, event: dict, args: str) -> None:
        import httpx

        if not args:
            await self._reply(event, "Usage: !attach <number>\n\nRun !list first.")
            return

        try:
            index = int(args.split()[0]) - 1
        except ValueError:
            await self._reply(event, "Please provide a session number.")
            return

        if not self._cached_external:
            await self._reply(event, "No external sessions cached. Run !list first.")
            return
        if not self._external_view:
            await self._reply(event, "No external sessions listed. Run !list first.")
            return
        if index < 0 or index >= len(self._external_view):
            await self._reply(
                event, f"Invalid number. Use 1–{len(self._external_view)}."
            )
            return

        external = self._external_view[index]

        try:
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

            # Check if already has a thread
            if session_id in self._thread_ts:
                await self._reply(
                    event, "Already attached — check the existing thread."
                )
                return

            # Create thread
            session_name = self._make_external_thread_name(
                directory=external.get("directory", ""),
                session_id=session_id,
            )
            thread_info = await self.create_thread(session_id, session_name)
            try:
                thread_ts = str(
                    thread_info.get("thread_ts") or thread_info.get("thread_id") or ""
                )
                if thread_ts:
                    await self._send_external_session_replay(
                        thread_ts=thread_ts,
                        external_id=external["id"],
                        runner_type=str(external["runner_type"]),
                    )
            except Exception:
                logger.exception(
                    "Failed to replay external session history into Slack thread"
                )

            # Bind platform
            from tether.store import store
            from tether.bridges.subscriber import bridge_subscriber

            db_session = store.get_session(session_id)
            if db_session:
                db_session.platform = "slack"
                db_session.platform_thread_id = thread_info.get("thread_id")
                store.update_session(db_session)

            bridge_subscriber.subscribe(session_id, "slack")

            dir_short = external.get("directory", "").rsplit("/", 1)[-1]
            await self._reply(
                event,
                f"✅ Attached to {external['runner_type']} session in {dir_short}\n\n"
                f"A new thread has been created — send messages there to interact.",
            )

        except httpx.HTTPStatusError as e:
            await self._reply(event, f"Failed to attach: {e.response.text}")
        except Exception as e:
            logger.exception("Failed to attach to external session")
            await self._reply(event, f"Failed to attach: {e}")

    async def _cmd_stop(self, event: dict) -> None:
        import httpx

        thread_ts = event.get("thread_ts")
        if not thread_ts:
            await self._reply(event, "Use this command inside a session thread.")
            return

        session_id = self._session_for_thread(thread_ts)
        if not session_id:
            await self._reply(event, "No session linked to this thread.")
            return

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self._api_url(f"/sessions/{session_id}/interrupt"),
                    headers=self._api_headers(),
                    timeout=10.0,
                )
                response.raise_for_status()
            await self._reply(event, "⏹️ Session interrupted.")
        except httpx.HTTPStatusError as e:
            try:
                error = e.response.json().get("error", {}).get("message", str(e))
            except Exception:
                error = str(e)
            await self._reply(event, f"Cannot interrupt: {error}")
        except Exception as e:
            logger.exception("Failed to interrupt session")
            await self._reply(event, f"Failed to interrupt: {e}")

    async def _cmd_usage(self, event: dict) -> None:
        """Show token usage for the session in the current thread."""
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            await self._reply(event, "Use this command inside a session thread.")
            return

        session_id = self._session_for_thread(thread_ts)
        if not session_id:
            await self._reply(event, "No session linked to this thread.")
            return

        try:
            usage = await self._fetch_usage(session_id)
            await self._reply(event, f"📊 {self._format_usage_text(usage)}")
        except Exception as e:
            logger.exception("Failed to get usage")
            await self._reply(event, f"Failed to get usage: {e}")

    # ------------------------------------------------------------------
    # Session input forwarding
    # ------------------------------------------------------------------

    async def _forward_input(self, event: dict, session_id: str, text: str) -> None:
        import httpx

        # Check if this is an approval response (allow/deny) for a pending permission
        pending = self.get_pending_permission(session_id)
        if pending:
            parsed = self.parse_approval_text(text)
            if parsed is not None:
                await self._handle_approval_text(event, session_id, pending, parsed)
                return

        try:
            await self._send_input_or_start_via_api(session_id=session_id, text=text)
            logger.info(
                "Forwarded human input from Slack",
                session_id=session_id,
                user=event.get("user"),
            )
        except httpx.HTTPStatusError as e:
            try:
                data = e.response.json()
                msg = (data.get("error") or {}).get("message") or e.response.text
            except Exception:
                msg = e.response.text
            await self._reply(event, f"Failed to send input: {msg}")
        except Exception:
            logger.exception("Failed to forward human input", session_id=session_id)
            await self._reply(event, "Failed to send input.")

    async def _handle_approval_text(
        self, event: dict, session_id: str, request: ApprovalRequest, parsed: dict
    ) -> None:
        """Handle a parsed approval text response."""
        allow = parsed["allow"]
        reason = parsed.get("reason")
        timer = parsed.get("timer")

        if allow and timer == "all":
            self.set_allow_all(session_id)
        elif allow and timer:
            self.set_allow_tool(session_id, timer)

        if allow:
            message = "Approved"
            if timer == "all":
                message = "Allow All (30m)"
            elif timer:
                message = f"Allow {timer} (30m)"
        else:
            message = f"Denied: {reason}" if reason else "Denied"

        ok = await self._respond_to_permission(
            session_id,
            request.request_id,
            allow=allow,
            message=message,
        )
        if ok:
            if allow:
                await self._reply(event, f"✅ {message}")
            else:
                await self._reply(event, f"❌ {message}")
        else:
            await self._reply(event, "❌ Failed — request may have expired.")

    # ------------------------------------------------------------------
    # Bridge interface (outgoing events)
    # ------------------------------------------------------------------

    async def on_output(
        self, session_id: str, text: str, metadata: dict | None = None
    ) -> None:
        """Send output text to Slack thread."""
        if not self._client:
            logger.warning("Slack client not initialized")
            return

        thread_ts = self._thread_ts.get(session_id)
        if not thread_ts:
            logger.warning("No Slack thread for session", session_id=session_id)
            return

        try:
            await self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text=text,
            )
        except Exception:
            logger.exception("Failed to send Slack message", session_id=session_id)

    async def on_approval_request(
        self, session_id: str, request: ApprovalRequest
    ) -> None:
        """Send an approval request to Slack thread."""
        if not self._client:
            return

        # Auto-approve if timer is active
        reason = self.check_auto_approve(session_id, request.title)
        if reason:
            await self._auto_approve(session_id, request, reason=reason)
            thread_ts = self._thread_ts.get(session_id)
            if thread_ts:
                try:
                    await self._client.chat_postMessage(
                        channel=self._channel_id,
                        thread_ts=thread_ts,
                        text=f"✅ *{request.title}* — auto-approved ({reason})",
                    )
                except Exception:
                    pass
            return

        thread_ts = self._thread_ts.get(session_id)
        if not thread_ts:
            return

        self.set_pending_permission(session_id, request)

        text = (
            f"*⚠️ Approval Required*\n\n*{request.title}*\n\n{request.description}\n\n"
            "Reply with `allow`, `deny`, `deny: <reason>`, `allow all`, or `allow {tool}`."
        )
        try:
            await self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text=text,
            )
        except Exception:
            logger.exception(
                "Failed to send Slack approval request", session_id=session_id
            )

    async def on_status_change(
        self, session_id: str, status: str, metadata: dict | None = None
    ) -> None:
        """Send status change to Slack thread."""
        if not self._client:
            return

        thread_ts = self._thread_ts.get(session_id)
        if not thread_ts:
            return

        emoji_map = {
            "thinking": ":thought_balloon:",
            "executing": ":gear:",
            "done": ":white_check_mark:",
            "error": ":x:",
        }
        emoji = emoji_map.get(status, ":information_source:")
        text = f"{emoji} Status: {status}"

        try:
            await self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text=text,
            )
        except Exception:
            logger.exception("Failed to send Slack status", session_id=session_id)

    async def create_thread(self, session_id: str, session_name: str) -> dict:
        """Create a Slack thread for a session."""
        if not self._client:
            raise RuntimeError("Slack client not initialized")

        try:
            response = await self._client.chat_postMessage(
                channel=self._channel_id,
                text=f"*New Session:* {session_name}",
            )

            if not response["ok"]:
                raise RuntimeError(f"Slack API error: {response}")

            thread_ts = response["ts"]
            self._thread_ts[session_id] = thread_ts

            logger.info(
                "Created Slack thread",
                session_id=session_id,
                thread_ts=thread_ts,
                name=session_name,
            )

            return {
                "thread_id": thread_ts,
                "platform": "slack",
                "thread_ts": thread_ts,
            }

        except Exception as e:
            logger.exception("Failed to create Slack thread", session_id=session_id)
            raise RuntimeError(f"Failed to create Slack thread: {e}")
