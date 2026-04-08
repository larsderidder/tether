"""Discord bridge wrapper with Tether-specific output normalization."""

from __future__ import annotations

from collections import deque
import os
import socket
from typing import Any

import structlog

from agent_tether.discord.bot import DiscordBridge as _BaseDiscordBridge
from agent_tether.discord.bot import DiscordConfig

__all__ = ["DiscordBridge", "DiscordConfig"]

logger = structlog.get_logger(__name__)
_HOSTNAME = socket.gethostname()


def _normalize_discord_output_text(text: str) -> str:
    """Normalize markdown list markers to render reliably in Discord.

    Discord markdown occasionally collapses dash-based list items in long
    assistant responses. Converting markdown list markers to unicode bullets
    keeps visual list structure stable while preserving plain text readability.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    marker_count = sum(
        1
        for line in lines
        if line.lstrip().startswith("- ") or line.lstrip().startswith("* ")
    )
    if marker_count < 2:
        return normalized

    converted: list[str] = []
    in_code_block = False
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            converted.append(line)
            continue

        if not in_code_block and (stripped.startswith("- ") or stripped.startswith("* ")):
            converted.append(f"{indent}• {stripped[2:]}")
        else:
            converted.append(line)

    return "\n".join(converted)


class DiscordBridge(_BaseDiscordBridge):
    """Tether Discord bridge wrapper."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Guard against duplicate delivery/replay of the same Discord message
        # event within this process.
        self._recent_message_ids: set[int] = set()
        self._recent_message_order: deque[int] = deque()
        self._recent_message_limit = 2048

    async def _handle_message(self, message: Any) -> None:
        message_id = getattr(message, "id", None)
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        channel_id = getattr(channel, "id", None)
        author_id = getattr(author, "id", None)
        content = (getattr(message, "content", "") or "").strip()
        command = content.split(None, 1)[0].lower() if content.startswith("!") else None
        if isinstance(message_id, int):
            if message_id in self._recent_message_ids:
                logger.warning(
                    "Ignoring duplicate Discord message event",
                    message_id=message_id,
                    channel_id=channel_id,
                    author_id=author_id,
                    command=command,
                    pid=os.getpid(),
                    host=_HOSTNAME,
                )
                return
            logger.info(
                "Handling Discord message event",
                message_id=message_id,
                channel_id=channel_id,
                author_id=author_id,
                command=command,
                pid=os.getpid(),
                host=_HOSTNAME,
            )
            self._recent_message_ids.add(message_id)
            self._recent_message_order.append(message_id)
            if len(self._recent_message_order) > self._recent_message_limit:
                old = self._recent_message_order.popleft()
                self._recent_message_ids.discard(old)
        await super()._handle_message(message)

    async def _cmd_sync(self, message: Any) -> None:
        """Handle !sync with optional force replay."""
        import discord

        text = (getattr(message, "content", "") or "").strip().lower()
        parts = text.split()
        force = any(arg in {"force", "--force", "-f"} for arg in parts[1:])

        if force and self._callbacks and self._callbacks.sync_session:
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
                result = await self._callbacks.sync_session(session_id, force=True)
            except TypeError:
                # Backward compatibility with older callback signatures.
                await super()._cmd_sync(message)
                return

            synced = result.get("synced", 0)
            total = result.get("total", 0)
            if synced:
                await message.channel.send(
                    f"🔄 Force-synced {synced} message(s) ({total} total)."
                )
            else:
                await message.channel.send(
                    f"✅ Nothing to replay ({total} message(s) total)."
                )
            return

        await super()._cmd_sync(message)

    async def on_output(self, session_id: str, text: str, metadata: dict | None = None) -> None:
        normalized = _normalize_discord_output_text(text)
        await super().on_output(session_id, normalized, metadata)
