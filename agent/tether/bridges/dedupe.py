"""Short-lived inbound bridge message dedupe and loop guards."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
import hashlib
import time
from typing import Any


class ShortLivedMessageDedupe:
    """Remember inbound platform message keys for a bounded time window."""

    def __init__(
        self,
        *,
        ttl_s: float = 30.0,
        max_entries: int = 2048,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._clock = clock or time.monotonic
        self._seen: OrderedDict[str, float] = OrderedDict()

    def seen_recently(self, key: str | None) -> bool:
        """Return true when key is still inside the dedupe window."""

        if not key:
            return False
        now = self._clock()
        self._prune(now)
        previous = self._seen.get(key)
        self._seen[key] = now
        self._seen.move_to_end(key)
        return previous is not None and now - previous <= self.ttl_s

    def _prune(self, now: float) -> None:
        cutoff = now - self.ttl_s
        while self._seen:
            _, timestamp = next(iter(self._seen.items()))
            if timestamp > cutoff and len(self._seen) <= self.max_entries:
                break
            self._seen.popitem(last=False)


def stable_message_fingerprint(*parts: object) -> str:
    """Build a stable fallback key when a platform message id is unavailable."""

    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part or "").encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return digest.hexdigest()[:24]


def discord_message_key(message: Any) -> str:
    """Build a Discord dedupe key from id or stable message fields."""

    message_id = str(getattr(message, "id", "") or "").strip()
    if message_id:
        return f"discord:{message_id}"
    channel = getattr(message, "channel", None)
    author = getattr(message, "author", None)
    attachments = getattr(message, "attachments", None) or []
    attachment_ids = ",".join(
        str(getattr(attachment, "id", "") or getattr(attachment, "filename", ""))
        for attachment in attachments
    )
    return "discord:fallback:" + stable_message_fingerprint(
        getattr(channel, "id", ""),
        getattr(author, "id", ""),
        getattr(message, "content", ""),
        attachment_ids,
    )


def telegram_update_key(update: Any) -> str:
    """Build a Telegram dedupe key from message id or stable message fields."""

    message = getattr(update, "message", None)
    if message is None:
        return ""
    chat = getattr(message, "chat", None)
    message_id = str(getattr(message, "message_id", "") or "").strip()
    chat_id = str(getattr(chat, "id", "") or "").strip()
    if chat_id and message_id:
        return f"telegram:{chat_id}:{message_id}"
    document = getattr(message, "document", None)
    return "telegram:fallback:" + stable_message_fingerprint(
        chat_id,
        getattr(message, "media_group_id", ""),
        getattr(message, "caption", ""),
        getattr(document, "file_unique_id", ""),
        getattr(document, "file_name", ""),
    )


def is_obvious_discord_bot_loop(message: Any) -> bool:
    """Return true for Discord messages that should not be bridged back to agents."""

    author = getattr(message, "author", None)
    if bool(getattr(author, "bot", False)):
        return True
    return bool(getattr(message, "webhook_id", None))


def is_obvious_telegram_bot_loop(update: Any) -> bool:
    """Return true for Telegram messages that should not be bridged back to agents."""

    message = getattr(update, "message", None)
    if message is None:
        return False
    from_user = getattr(message, "from_user", None)
    if bool(getattr(from_user, "is_bot", False)):
        return True
    via_bot = getattr(message, "via_bot", None)
    return bool(getattr(via_bot, "is_bot", False))
