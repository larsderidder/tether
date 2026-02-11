"""Discord bridge implementation with command handling and session threading."""

from pathlib import Path

import structlog

from tether.bridges.base import (
    ApprovalRequest,
    BridgeInterface,
    _EXTERNAL_MAX_FETCH,
    _EXTERNAL_REPLAY_LIMIT,
    _EXTERNAL_REPLAY_MAX_CHARS,
)
from tether.bridges.discord.pairing_state import (
    DiscordPairingState,
    load_or_create as load_pairing_state,
    save as save_pairing_state,
)
from tether.bridges.thread_state import load_mapping, save_mapping
from tether.settings import settings

logger = structlog.get_logger(__name__)

_DISCORD_THREAD_NAME_MAX_LEN = 64
_DISCORD_MSG_LIMIT = 2000


class DiscordBridge(BridgeInterface):
    """Discord bridge that routes agent events to Discord threads.

    Commands (in main channel): !help, !status, !list, !attach, !stop, !usage
    Session input: messages in session threads are forwarded as input.
    """

    def __init__(self, bot_token: str, channel_id: int):
        super().__init__()
        self._bot_token = bot_token
        self._channel_id = channel_id
        self._client: any = None
        self._thread_ids: dict[str, int] = {}  # session_id -> thread_id
        self._thread_name_path = Path(settings.data_dir()) / "discord_threads.json"
        self._thread_names: dict[str, str] = load_mapping(path=self._thread_name_path)
        self._used_thread_names: set[str] = set(self._thread_names.values())
        # Pairing / allowlist
        self._pairing_required = settings.discord_require_pairing()
        self._allowed_user_ids = settings.discord_allowed_user_ids()
        self._pairing_state_path = Path(settings.data_dir()) / "discord_pairing.json"
        self._pairing_state: DiscordPairingState | None = None
        self._paired_user_ids: set[int] = set()
        self._pairing_code: str | None = None

        fixed_code = settings.discord_pairing_code() or None
        if self._pairing_required or fixed_code:
            self._pairing_state = load_pairing_state(
                path=self._pairing_state_path,
                fixed_code=fixed_code,
            )
            self._paired_user_ids = set(self._pairing_state.paired_user_ids)
            self._pairing_code = self._pairing_state.pairing_code
            if not self._channel_id and self._pairing_state.control_channel_id:
                self._channel_id = int(self._pairing_state.control_channel_id)
        elif not self._channel_id:
            # Even without explicit pairing requirement, if the channel isn't set we
            # still want a setup code to prevent random users from configuring it.
            self._pairing_state = load_pairing_state(
                path=self._pairing_state_path,
                fixed_code=fixed_code,
            )
            self._paired_user_ids = set(self._pairing_state.paired_user_ids)
            self._pairing_code = self._pairing_state.pairing_code
            if self._pairing_state.control_channel_id:
                self._channel_id = int(self._pairing_state.control_channel_id)

    def restore_thread_mappings(self) -> None:
        """Restore session-to-thread mappings from the store after restart."""
        from tether.store import store

        for session in store.list_sessions():
            if session.platform == "discord" and session.platform_thread_id:
                try:
                    self._thread_ids[session.id] = int(session.platform_thread_id)
                except (ValueError, TypeError):
                    pass

    async def start(self) -> None:
        """Initialize and start Discord client."""
        try:
            import discord
        except ImportError:
            logger.error(
                "discord.py not installed. Install with: pip install discord.py"
            )
            return

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_ready():
            logger.info("Discord client ready", user=self._client.user)

        @self._client.event
        async def on_message(message):
            await self._handle_message(message)

        import asyncio

        asyncio.create_task(self._client.start(self._bot_token))

        logger.info(
            "Discord bridge initialized and starting", channel_id=self._channel_id
        )
        if not self._channel_id and self._pairing_code:
            logger.warning(
                "Discord bridge not configured with a control channel. Run !setup <code> in the desired channel.",
                code=self._pairing_code,
            )
        elif self._pairing_required and self._pairing_code:
            logger.warning(
                "Discord pairing enabled. DM the bot: !pair <code>",
                code=self._pairing_code,
            )

    async def stop(self) -> None:
        """Stop Discord client."""
        if self._client:
            await self._client.close()
        logger.info("Discord bridge stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pick_unique_thread_name(self, base_name: str) -> str:
        base_name = (base_name or "Session").strip() or "Session"
        base_name = base_name[:_DISCORD_THREAD_NAME_MAX_LEN]
        if base_name not in self._used_thread_names:
            return base_name

        for i in range(2, 100):
            suffix = f" {i}"
            avail = max(1, _DISCORD_THREAD_NAME_MAX_LEN - len(suffix))
            candidate = (base_name[:avail] + suffix)[:_DISCORD_THREAD_NAME_MAX_LEN]
            if candidate not in self._used_thread_names:
                return candidate

        return base_name

    def _is_authorized_user_id(self, user_id: int | None) -> bool:
        if not user_id:
            return False
        if int(user_id) in self._allowed_user_ids:
            return True
        if int(user_id) in self._paired_user_ids:
            return True

        # Backwards-compatible default: if pairing isn't required and no allowlist
        # is configured and no-one has paired yet, allow all users in the channel.
        if (
            not self._pairing_required
            and not self._allowed_user_ids
            and not self._paired_user_ids
        ):
            return True
        return False

    async def _send_not_paired(self, message: any) -> None:
        if not self._pairing_required:
            await message.channel.send(
                "🔒 Not authorized. Pairing is not required, but an allowlist/pairing may be configured."
            )
            return
        await message.channel.send(
            "🔒 Not paired. DM the bot: `!pair <code>` (pairing code is in the Tether server logs)."
        )

    def _ensure_pairing_state_loaded(self) -> None:
        if self._pairing_state:
            return
        fixed_code = settings.discord_pairing_code() or None
        self._pairing_state = load_pairing_state(
            path=self._pairing_state_path,
            fixed_code=fixed_code,
        )
        self._paired_user_ids = set(self._pairing_state.paired_user_ids)
        self._pairing_code = self._pairing_state.pairing_code
        if not self._channel_id and self._pairing_state.control_channel_id:
            self._channel_id = int(self._pairing_state.control_channel_id)

    def _make_external_thread_name(self, *, directory: str, session_id: str) -> str:
        # Match Telegram's naming style: directory name, upper-cased, and ensure
        # uniqueness by appending numbers ("Repo", "Repo 2", ...).
        dir_short = (directory or "").rstrip("/").rsplit("/", 1)[-1] or "Session"
        base_name = (dir_short[:1].upper() + dir_short[1:])[
            :_DISCORD_THREAD_NAME_MAX_LEN
        ]
        return self._pick_unique_thread_name(base_name)

    async def _send_external_session_replay(
        self, *, thread_id: int, external_id: str, runner_type: str
    ) -> None:
        """Send recent external session history into the Discord thread."""
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
            f"Recent history (last {min(_EXTERNAL_REPLAY_LIMIT, len(messages))} messages):\n"
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
            thread = self._client.get_channel(thread_id)
            if thread:
                await thread.send(text)
        except Exception:
            logger.exception(
                "Failed to send Discord external session replay",
                external_id=external_id,
            )

    def _session_for_thread(self, thread_id: int) -> str | None:
        for sid, tid in self._thread_ids.items():
            if tid == thread_id:
                return sid
        return None

    # ------------------------------------------------------------------
    # Message router
    # ------------------------------------------------------------------

    async def _handle_message(self, message: any) -> None:
        """Route incoming Discord messages to commands or session input."""
        try:
            import discord
        except ImportError:
            return

        if message.author.bot:
            return

        text = message.content.strip()
        if not text:
            return

        # Setup/pairing commands are allowed even when not authorized.
        if text.lower().startswith(("!pair", "!setup")):
            await self._dispatch_command(message, text)
            return

        # Messages in threads → session input or thread commands
        if isinstance(message.channel, discord.Thread):
            if text.startswith("!"):
                await self._dispatch_command(message, text)
                return
            session_id = self._session_for_thread(message.channel.id)
            if not session_id:
                return
            if not self._is_authorized_user_id(getattr(message.author, "id", None)):
                await self._send_not_paired(message)
                return
            await self._forward_input(message, session_id, text)
            return

        # Messages in the configured channel starting with ! → commands
        if self._channel_id and message.channel.id == self._channel_id and text.startswith("!"):
            await self._dispatch_command(message, text)
            return

        # If not configured, allow running !setup in any non-thread channel.
        if not self._channel_id and text.lower().startswith("!setup"):
            await self._dispatch_command(message, text)

    async def _dispatch_command(self, message: any, text: str) -> None:
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd not in ("!help", "!start", "!pair", "!pair-status", "!setup") and not self._is_authorized_user_id(
            getattr(message.author, "id", None)
        ):
            await self._send_not_paired(message)
            return

        if cmd in ("!help", "!start"):
            await self._cmd_help(message)
        elif cmd in ("!status", "!sessions"):
            await self._cmd_status(message)
        elif cmd == "!list":
            await self._cmd_list(message, args)
        elif cmd == "!attach":
            await self._cmd_attach(message, args)
        elif cmd == "!new":
            await self._cmd_new(message, args)
        elif cmd == "!stop":
            await self._cmd_stop(message)
        elif cmd == "!usage":
            await self._cmd_usage(message)
        elif cmd == "!pair":
            await self._cmd_pair(message, args)
        elif cmd == "!pair-status":
            await self._cmd_pair_status(message)
        elif cmd == "!setup":
            await self._cmd_setup(message, args)
        else:
            await message.channel.send(
                f"Unknown command: {cmd}\nUse !help for available commands."
            )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _cmd_help(self, message: any) -> None:
        text = (
            "Tether Commands:\n\n"
            "!status — List all sessions\n"
            "!list [page|search] — List external sessions (Claude Code, Codex)\n"
            "!attach <number> [force] — Attach to an external session\n"
            "!new [agent] [directory] — Start a new session\n"
            "!stop — Interrupt the session in this thread\n"
            "!usage — Show token usage and cost for this session\n"
            "!setup <code> — Configure this channel as the control channel and pair you\n"
            "!pair <code> — Pair your Discord user to authorize commands\n"
            "!pair-status — Show whether you are authorized\n"
            "!help — Show this help\n\n"
            "Send a text message in a session thread to forward it as input."
        )
        await message.channel.send(text)

    async def _cmd_setup(self, message: any, args: str) -> None:
        """Configure the current channel as the bot's control channel.

        This avoids requiring users to copy a channel ID manually.
        """
        code = (args or "").strip()
        if not code:
            await message.channel.send("Usage: `!setup <code>`")
            return

        self._ensure_pairing_state_loaded()
        if not self._pairing_code or code != self._pairing_code:
            await message.channel.send("Invalid setup code.")
            return

        # Record this channel as the control channel.
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        if not channel_id:
            await message.channel.send("Could not read this channel id.")
            return

        self._channel_id = int(channel_id)
        assert self._pairing_state is not None
        self._pairing_state.control_channel_id = self._channel_id

        # Pair the caller as well (so they can immediately use the bot).
        user_id = getattr(getattr(message, "author", None), "id", None)
        if user_id:
            self._paired_user_ids.add(int(user_id))
            self._pairing_state.paired_user_ids = set(self._paired_user_ids)

        save_pairing_state(path=self._pairing_state_path, state=self._pairing_state)

        await message.channel.send(
            "✅ Setup complete. This channel is now the control channel. Try `!help`."
        )

    async def _cmd_pair(self, message: any, args: str) -> None:
        # Allow pairing via DM, or from the configured control channel.
        guild = getattr(message, "guild", None)
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        if guild is not None and channel_id != self._channel_id:
            return

        if not (self._pairing_required or settings.discord_pairing_code()):
            await message.channel.send(
                "Pairing is not enabled. Set `DISCORD_REQUIRE_PAIRING=1` to enforce it."
            )
            return

        code = (args or "").strip()
        if not code:
            await message.channel.send("Usage: `!pair <code>`")
            return

        self._ensure_pairing_state_loaded()
        if not self._pairing_code or code != self._pairing_code:
            await message.channel.send("Invalid pairing code.")
            return

        user_id = getattr(message.author, "id", None)
        if not user_id:
            await message.channel.send("Could not read your Discord user id.")
            return

        self._paired_user_ids.add(int(user_id))
        assert self._pairing_state is not None
        self._pairing_state.paired_user_ids = set(self._paired_user_ids)
        save_pairing_state(path=self._pairing_state_path, state=self._pairing_state)

        await message.channel.send("✅ Paired. You can now use Tether commands.")

    async def _cmd_pair_status(self, message: any) -> None:
        user_id = getattr(getattr(message, "author", None), "id", None)
        authorized = self._is_authorized_user_id(user_id)
        await message.channel.send(
            f"Pairing required: {self._pairing_required}\n"
            f"Authorized: {authorized}\n"
            f"Your user id: {user_id}"
        )

    async def _cmd_new(self, message: any, args: str) -> None:
        """Create a new session and Discord thread.

        Usage:
        - In a session thread:
          - !new
          - !new <agent>
          - !new <directory-name>
        - In main channel:
          - !new <agent> <directory>
          - !new <directory>
        """
        try:
            import discord
        except ImportError:
            return

        parts = (args or "").split()

        base_session_id: str | None = None
        base_directory: str | None = None
        base_adapter: str | None = None
        if isinstance(message.channel, discord.Thread):
            base_session_id = self._session_for_thread(message.channel.id)
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
                await message.channel.send(
                    "Usage: !new <agent> <directory>\nOr, inside a session thread: !new or !new <agent>"
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
                    await message.channel.send("Usage: !new <agent> <directory>")
                    return
                directory_raw = token
        else:
            adapter = self._agent_to_adapter(parts[0])
            if not adapter:
                await message.channel.send(
                    "Unknown agent. Use: claude, codex, claude_auto, claude_subprocess, claude_api, codex_sdk_sidecar"
                )
                return
            directory_raw = " ".join(parts[1:]).strip()

        try:
            assert directory_raw is not None
            directory = await self._resolve_directory_arg(
                directory_raw, base_directory=base_directory
            )
        except Exception as e:
            await message.channel.send(f"Invalid directory: {e}")
            return

        dir_short = directory.rstrip("/").rsplit("/", 1)[-1] or "Session"
        agent_label = self._adapter_label(adapter) or self._adapter_label(settings.adapter()) or "Claude"
        session_name = self._make_external_thread_name(directory=directory, session_id="")

        try:
            session = await self._create_session_via_api(
                directory=directory,
                platform="discord",
                adapter=adapter,
                session_name=session_name,
            )
        except Exception as e:
            await message.channel.send(f"Failed to create session: {e}")
            return

        await message.channel.send(f"✅ New {agent_label} session created in {dir_short}.")
        try:
            thread_id = int(session.get("platform_thread_id") or 0)
        except Exception:
            thread_id = 0
        if thread_id:
            # Mention the thread so it's easy to find.
            await message.channel.send(f"🧵 Open thread: <#{thread_id}>")

    async def _cmd_status(self, message: any) -> None:
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
            await message.channel.send("Failed to fetch sessions.")
            return

        if not sessions:
            await message.channel.send("No sessions.")
            return

        lines = ["Sessions:\n"]
        for s in sessions:
            emoji = self._STATE_EMOJI.get(s.get("state", ""), "❓")
            name = s.get("name") or s.get("id", "")[:12]
            lines.append(f"  {emoji} {name}")
        await message.channel.send("\n".join(lines))

    async def _cmd_list(self, message: any, args: str) -> None:
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
            await message.channel.send("Failed to list external sessions.")
            return

        text, _, _ = self._format_external_page(page)
        await message.channel.send(text)

    async def _cmd_attach(self, message: any, args: str) -> None:
        import httpx

        if not args:
            await message.channel.send("Usage: !attach <number> [force]\n\nRun !list first.")
            return

        parts = args.split()
        force = len(parts) > 1 and parts[-1].lower() == "force"

        try:
            index = int(parts[0]) - 1
        except ValueError:
            await message.channel.send("Please provide a session number.")
            return

        if not self._cached_external:
            await message.channel.send("No external sessions cached. Run !list first.")
            return
        if not self._external_view:
            await message.channel.send("No external sessions listed. Run !list first.")
            return
        if index < 0 or index >= len(self._external_view):
            await message.channel.send(
                f"Invalid number. Use 1–{len(self._external_view)}."
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
            existing_thread_id = self._thread_ids.get(session_id)
            if existing_thread_id:
                if force:
                    logger.info(
                        "Force-recreating thread",
                        session_id=session_id,
                        thread_id=existing_thread_id,
                    )
                    self._thread_ids.pop(session_id, None)
                    name = self._thread_names.pop(session_id, None)
                    if name:
                        self._used_thread_names.discard(name)
                        save_mapping(path=self._thread_name_path, mapping=self._thread_names)
                else:
                    # Verify the thread is still accessible
                    thread_ok = False
                    try:
                        thread = self._client.get_channel(existing_thread_id)
                        if thread is not None:
                            thread_ok = True
                    except Exception:
                        pass

                    if thread_ok:
                        await message.channel.send(
                            f"Already attached. Open thread: <#{existing_thread_id}>\n"
                            "Use `!attach <number> force` to recreate the thread."
                        )
                        return
                    else:
                        logger.info(
                            "Existing thread is stale, will recreate",
                            session_id=session_id,
                            thread_id=existing_thread_id,
                        )
                        self._thread_ids.pop(session_id, None)
                        name = self._thread_names.pop(session_id, None)
                        if name:
                            self._used_thread_names.discard(name)
                            save_mapping(path=self._thread_name_path, mapping=self._thread_names)

            # Create thread
            session_name = self._make_external_thread_name(
                directory=external.get("directory", ""),
                session_id=session_id,
            )
            thread_info = await self.create_thread(session_id, session_name)
            try:
                thread_id = int(thread_info.get("thread_id") or 0)
                if thread_id:
                    # Post a clickable link to the thread so users can find it easily.
                    await message.channel.send(f"🧵 Open thread: <#{thread_id}>")
                    await self._send_external_session_replay(
                        thread_id=thread_id,
                        external_id=external["id"],
                        runner_type=str(external["runner_type"]),
                    )
            except Exception:
                logger.exception(
                    "Failed to replay external session history into Discord thread"
                )

            # Bind platform
            from tether.store import store
            from tether.bridges.subscriber import bridge_subscriber

            db_session = store.get_session(session_id)
            if db_session:
                db_session.platform = "discord"
                db_session.platform_thread_id = thread_info.get("thread_id")
                store.update_session(db_session)

            bridge_subscriber.subscribe(session_id, "discord")

            dir_short = external.get("directory", "").rsplit("/", 1)[-1]
            await message.channel.send(
                f"✅ Attached to {external['runner_type']} session in {dir_short}\n\n"
                f"A new thread has been created. Send messages there to interact."
            )

        except httpx.HTTPStatusError as e:
            await message.channel.send(f"Failed to attach: {e.response.text}")
        except Exception as e:
            logger.exception("Failed to attach to external session")
            await message.channel.send(f"Failed to attach: {e}")

    async def _cmd_stop(self, message: any) -> None:
        import discord
        import httpx

        if not isinstance(message.channel, discord.Thread):
            await message.channel.send("Use this command inside a session thread.")
            return
        if not self._is_authorized_user_id(getattr(message.author, "id", None)):
            await self._send_not_paired(message)
            return

        session_id = self._session_for_thread(message.channel.id)
        if not session_id:
            await message.channel.send("No session linked to this thread.")
            return

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self._api_url(f"/sessions/{session_id}/interrupt"),
                    headers=self._api_headers(),
                    timeout=10.0,
                )
                response.raise_for_status()
            await message.channel.send("⏹️ Session interrupted.")
        except httpx.HTTPStatusError as e:
            try:
                error = e.response.json().get("error", {}).get("message", str(e))
            except Exception:
                error = str(e)
            await message.channel.send(f"Cannot interrupt: {error}")
        except Exception as e:
            logger.exception("Failed to interrupt session")
            await message.channel.send(f"Failed to interrupt: {e}")

    async def _cmd_usage(self, message: any) -> None:
        """Show token usage for the session in the current thread."""
        try:
            import discord
        except ImportError:
            return

        if not isinstance(message.channel, discord.Thread):
            await message.channel.send("Use this command inside a session thread.")
            return

        session_id = self._session_for_thread(message.channel.id)
        if not session_id:
            await message.channel.send("No session linked to this thread.")
            return

        try:
            usage = await self._fetch_usage(session_id)
            await message.channel.send(f"📊 {self._format_usage_text(usage)}")
        except Exception as e:
            logger.exception("Failed to get usage")
            await message.channel.send(f"Failed to get usage: {e}")

    # ------------------------------------------------------------------
    # Session input forwarding
    # ------------------------------------------------------------------

    async def _forward_input(self, message: any, session_id: str, text: str) -> None:
        import httpx

        if not self._is_authorized_user_id(getattr(message.author, "id", None)):
            await self._send_not_paired(message)
            return

        # Check if this is an approval response for a pending permission
        pending = self.get_pending_permission(session_id)
        if pending:
            # Choice requests: allow "1"/"2"/... or exact label; send as normal input.
            if pending.kind == "choice":
                selected = self.parse_choice_text(session_id, text)
                if selected:
                    await self._send_input_or_start_via_api(
                        session_id=session_id, text=selected
                    )
                    self.clear_pending_permission(session_id)
                    await message.channel.send(f"✅ Selected: {selected}")
                    return

            parsed = self.parse_approval_text(text)
            if parsed is not None:
                await self._handle_approval_text(message, session_id, pending, parsed)
                return

        try:
            await self._send_input_or_start_via_api(session_id=session_id, text=text)
            logger.info(
                "Forwarded human input from Discord",
                session_id=session_id,
                thread_id=message.channel.id,
                username=message.author.name,
            )
        except httpx.HTTPStatusError as e:
            try:
                data = e.response.json()
                err = data.get("error") or {}
                code = err.get("code")
                msg = err.get("message") or e.response.text
                if code == "RUNNER_UNAVAILABLE":
                    msg = "Runner backend is not reachable. Start `codex-sdk-sidecar` and try again."
            except Exception:
                msg = e.response.text
            await message.channel.send(f"Failed to send input: {msg}")
        except Exception:
            logger.exception("Failed to forward human input", session_id=session_id)
            await message.channel.send("Failed to send input.")

    async def _handle_approval_text(
        self, message: any, session_id: str, request: ApprovalRequest, parsed: dict
    ) -> None:
        """Handle a parsed approval text response."""
        allow = parsed["allow"]
        reason = parsed.get("reason")
        timer = parsed.get("timer")

        if allow and timer == "all":
            self.set_allow_all(session_id)
        elif allow and timer == "dir":
            from tether.store import store as _store

            _sess = _store.get_session(session_id)
            if _sess and _sess.directory:
                self.set_allow_directory(_sess.directory)
            else:
                self.set_allow_all(session_id)
        elif allow and timer:
            self.set_allow_tool(session_id, timer)

        if allow:
            msg = "Approved"
            if timer == "all":
                msg = "Allow All (30m)"
            elif timer == "dir":
                msg = "Allow dir (30m)"
            elif timer:
                msg = f"Allow {timer} (30m)"
        else:
            msg = f"Denied: {reason}" if reason else "Denied"

        ok = await self._respond_to_permission(
            session_id,
            request.request_id,
            allow=allow,
            message=msg,
        )
        if ok:
            if allow:
                await message.channel.send(f"✅ {msg}")
            else:
                await message.channel.send(f"❌ {msg}")
        else:
            await message.channel.send("❌ Failed. Request may have expired.")

    # ------------------------------------------------------------------
    # Bridge interface (outgoing events)
    # ------------------------------------------------------------------

    async def on_output(
        self, session_id: str, text: str, metadata: dict | None = None
    ) -> None:
        """Send output text to Discord thread."""
        if not self._client:
            logger.warning("Discord client not initialized")
            return

        thread_id = self._thread_ids.get(session_id)
        if not thread_id:
            logger.warning("No Discord thread for session", session_id=session_id)
            return

        try:
            thread = self._client.get_channel(thread_id)
            if thread:
                # Discord has a 2000 char limit per message
                for i in range(0, len(text), _DISCORD_MSG_LIMIT):
                    await thread.send(text[i : i + _DISCORD_MSG_LIMIT])
        except Exception:
            logger.exception("Failed to send Discord message", session_id=session_id)

    async def send_auto_approve_batch(
        self, session_id: str, items: list[tuple[str, str]]
    ) -> None:
        """Send a batched auto-approve notification to Discord."""
        if not self._client:
            return
        thread_id = self._thread_ids.get(session_id)
        if not thread_id:
            return

        if len(items) == 1:
            tool_name, reason = items[0]
            text = f"✅ **{tool_name}** — auto-approved ({reason})"
        else:
            lines = [f"✅ Auto-approved {len(items)} tools:"]
            for tool_name, _reason in items:
                lines.append(f"  • {tool_name}")
            lines.append(f"*({items[0][1]})*")
            text = "\n".join(lines)

        try:
            thread = self._client.get_channel(thread_id)
            if thread:
                await thread.send(text[:_DISCORD_MSG_LIMIT])
        except Exception:
            pass

    async def on_approval_request(
        self, session_id: str, request: ApprovalRequest
    ) -> None:
        """Send an approval request to Discord thread."""
        if not self._client:
            return

        # Choice requests: send options and let user reply with "1"/"2"/... or the label.
        if request.kind == "choice":
            thread_id = self._thread_ids.get(session_id)
            if not thread_id:
                return
            thread = self._client.get_channel(thread_id)
            if not thread:
                return

            self.set_pending_permission(session_id, request)
            options = "\n".join(
                [f"{i}. {o}" for i, o in enumerate(request.options, start=1)]
            )
            text = (
                f"⚠️ **{request.title}**\n\n{request.description}\n\n{options}\n\n"
                "Reply with a number (e.g. `1`) or an exact option label."
            )
            try:
                await thread.send(text)
            except Exception:
                logger.exception(
                    "Failed to send Discord choice request", session_id=session_id
                )
            return

        reason: str | None = None
        if request.kind == "permission":
            reason = self.check_auto_approve(session_id, request.title)
        if reason:
            await self._auto_approve(session_id, request, reason=reason)
            self.buffer_auto_approve_notification(session_id, request.title, reason)
            return

        thread_id = self._thread_ids.get(session_id)
        if not thread_id:
            return

        self.set_pending_permission(session_id, request)

        formatted = self.format_tool_input_markdown(request.description)
        text = (
            f"**⚠️ Approval Required**\n\n**{request.title}**\n\n{formatted}\n\n"
            "Reply with `allow`/`proceed`, `deny`/`cancel`, `deny: <reason>`, `allow all`, or `allow {tool}`."
        )
        try:
            thread = self._client.get_channel(thread_id)
            if thread:
                await thread.send(text)
        except Exception:
            logger.exception(
                "Failed to send Discord approval request", session_id=session_id
            )

    async def on_status_change(
        self, session_id: str, status: str, metadata: dict | None = None
    ) -> None:
        """Send status change to Discord thread."""
        if not self._client:
            return

        if status == "error" and not self._should_send_error_status(session_id):
            return

        thread_id = self._thread_ids.get(session_id)
        if not thread_id:
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
            thread = self._client.get_channel(thread_id)
            if thread:
                await thread.send(text)
        except Exception:
            logger.exception("Failed to send Discord status", session_id=session_id)

    async def create_thread(self, session_id: str, session_name: str) -> dict:
        """Create a Discord thread for a session."""
        if not self._client:
            raise RuntimeError("Discord client not initialized")

        try:
            # Reserve name for uniqueness within this bridge instance and across restarts.
            self._thread_names[session_id] = session_name
            self._used_thread_names.add(session_name)
            save_mapping(path=self._thread_name_path, mapping=self._thread_names)

            channel = self._client.get_channel(self._channel_id)
            if not channel:
                raise RuntimeError(f"Discord channel {self._channel_id} not found")

            thread = await channel.create_thread(
                name=session_name[:100],  # Discord limit
                auto_archive_duration=1440,  # 24 hours
            )

            thread_id = thread.id
            self._thread_ids[session_id] = thread_id
            try:
                await thread.send(
                    "Tether session thread.\n"
                    "Send a message here to provide input. Use `!stop` to interrupt, `!usage` for token usage."
                )
            except Exception:
                # Thread creation succeeded; welcome message is best-effort.
                pass

            logger.info(
                "Created Discord thread",
                session_id=session_id,
                thread_id=thread_id,
                name=session_name,
            )

            return {
                "thread_id": str(thread_id),
                "platform": "discord",
            }

        except Exception as e:
            logger.exception("Failed to create Discord thread", session_id=session_id)
            # Best-effort rollback if thread creation failed.
            if self._thread_names.get(session_id) == session_name:
                self._thread_names.pop(session_id, None)
                self._used_thread_names.discard(session_name)
                save_mapping(path=self._thread_name_path, mapping=self._thread_names)
            raise RuntimeError(f"Failed to create Discord thread: {e}")

    async def on_session_removed(self, session_id: str) -> None:
        name = self._thread_names.pop(session_id, None)
        if name:
            self._used_thread_names.discard(name)
            save_mapping(path=self._thread_name_path, mapping=self._thread_names)
        await super().on_session_removed(session_id)
