"""Regression tests for real bridge delivery failure contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from agent_tether.base import ApprovalRequest
from agent_tether.manager import BridgeManager

from tether.bridges.subscriber import BridgeSubscriber
from tether.store import SessionStore


class PermanentSendError(Exception):
    """Fake non-retryable platform send failure."""

    status = 400


class FakeSlackClient:
    """Minimal Slack client that can fail all sends."""

    def __init__(self) -> None:
        self.fail = False
        self.messages: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail:
            raise PermanentSendError("slack send failed")
        self.messages.append(dict(kwargs))
        return {"ok": True, "ts": str(len(self.messages))}

    async def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail:
            raise PermanentSendError("slack upload failed")
        self.uploads.append(dict(kwargs))
        return {"ok": True}


class FakeTelegramBot:
    """Minimal Telegram bot that can fail all sends."""

    def __init__(self) -> None:
        self.fail = False
        self.messages: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> SimpleNamespace:
        if self.fail:
            raise PermanentSendError("telegram send failed")
        self.messages.append(dict(kwargs))
        return SimpleNamespace(message_id=len(self.messages))


class FakeDiscordThread:
    """Minimal Discord thread that can fail all sends."""

    id = 9876543210

    def __init__(self) -> None:
        self.fail = False
        self.messages: list[str] = []

    async def send(self, text: str, **kwargs: Any) -> SimpleNamespace:
        if self.fail:
            raise PermanentSendError("discord send failed")
        self.messages.append(text)
        return SimpleNamespace(id=len(self.messages), add_reaction=self._add_reaction)

    async def _add_reaction(self, reaction: str) -> None:
        return None


class FakeDiscordClient:
    """Minimal Discord client returning one fake thread."""

    def __init__(self, thread: FakeDiscordThread) -> None:
        self.thread = thread

    def get_channel(self, thread_id: int) -> FakeDiscordThread:
        return self.thread

    async def fetch_channel(self, thread_id: int) -> FakeDiscordThread:
        return self.thread


@pytest.fixture
def approval_request() -> ApprovalRequest:
    """Return a representative permission request."""

    return ApprovalRequest(
        kind="permission",
        request_id="perm_1",
        title="Read",
        description="Read a file",
        options=[],
    )


@pytest.fixture
def choice_request() -> ApprovalRequest:
    """Return a representative choice request."""

    return ApprovalRequest(
        kind="choice",
        request_id="choice_1",
        title="Pick one",
        description="Choose an option",
        options=["one", "two"],
    )


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["output", "permission", "choice", "status"])
async def test_slack_bridge_send_failures_propagate_then_recover(
    method: str,
    approval_request: ApprovalRequest,
    choice_request: ApprovalRequest,
) -> None:
    """Slack output, approval, choice, and status sends fail loudly, then recover."""

    from tether.bridges.slack.bot import SlackBridge

    bridge = SlackBridge(bot_token="xoxb-test-token", channel_id="C123")
    client = FakeSlackClient()
    bridge._client = client
    bridge._thread_ts["sess_1"] = "111.222"

    async def call_method() -> None:
        if method == "output":
            await bridge.on_output("sess_1", "hello")
        elif method == "permission":
            await bridge.on_approval_request("sess_1", approval_request)
        elif method == "choice":
            await bridge.on_approval_request("sess_1", choice_request)
        else:
            await bridge.on_status_change("sess_1", "done")

    client.fail = True
    with pytest.raises(PermanentSendError):
        await call_method()

    client.fail = False
    await call_method()

    assert client.messages


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["output", "permission", "choice", "status"])
async def test_telegram_bridge_send_failures_propagate_then_recover(
    method: str,
    approval_request: ApprovalRequest,
    choice_request: ApprovalRequest,
) -> None:
    """Telegram output, approval, choice, and status sends fail loudly, then recover."""

    from tether.bridges.telegram.bot import TelegramBridge

    bridge = TelegramBridge(bot_token="test-token", forum_group_id=-100123)
    bot = FakeTelegramBot()
    bridge._app = SimpleNamespace(bot=bot)
    bridge._state.set_topic_for_session("sess_1", 42, "Session")

    async def call_method() -> None:
        if method == "output":
            await bridge.on_output("sess_1", "hello")
        elif method == "permission":
            await bridge.on_approval_request("sess_1", approval_request)
        elif method == "choice":
            await bridge.on_approval_request("sess_1", choice_request)
        else:
            await bridge.on_status_change("sess_1", "done")

    bot.fail = True
    with pytest.raises(PermanentSendError):
        await call_method()

    bot.fail = False
    await call_method()

    assert bot.messages


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["output", "permission", "choice", "status"])
async def test_discord_bridge_send_failures_propagate_then_recover(
    method: str,
    approval_request: ApprovalRequest,
    choice_request: ApprovalRequest,
) -> None:
    """Discord output, approval, choice, and status sends fail loudly, then recover."""

    from tether.bridges.discord.bot import DiscordBridge

    thread = FakeDiscordThread()
    bridge = DiscordBridge(bot_token="discord-token", channel_id=123)
    bridge._client = FakeDiscordClient(thread)
    bridge._thread_ids["sess_1"] = thread.id

    async def call_method() -> None:
        if method == "output":
            await bridge.on_output("sess_1", "hello")
        elif method == "permission":
            await bridge.on_approval_request("sess_1", approval_request)
        elif method == "choice":
            await bridge.on_approval_request("sess_1", choice_request)
        else:
            await bridge.on_status_change("sess_1", "done")

    thread.fail = True
    with pytest.raises(PermanentSendError):
        await call_method()

    thread.fail = False
    await call_method()

    assert thread.messages


@pytest.mark.anyio
async def test_real_bridge_failure_keeps_outbox_pending_then_retry_sends(
    fresh_store: SessionStore,
) -> None:
    """A real bridge send failure keeps the outbox row pending until retry succeeds."""

    from tether.bridges.slack.bot import SlackBridge

    session = fresh_store.create_session("repo", "main")
    session.platform = "slack"
    session.platform_thread_id = "111.222"
    fresh_store.update_session(session)

    client = FakeSlackClient()
    client.fail = True
    bridge = SlackBridge(bot_token="xoxb-test-token", channel_id="C123")
    bridge._client = client
    bridge._thread_ts[session.id] = "111.222"

    manager = BridgeManager()
    manager.register_bridge("slack", bridge)
    subscriber = BridgeSubscriber(
        bridge_manager=manager,
        new_subscriber=fresh_store.new_subscriber,
        remove_subscriber=fresh_store.remove_subscriber,
        outbox_retry_delay_s=0,
    )

    sent = await subscriber._enqueue_and_deliver(
        session_id=session.id,
        platform="slack",
        bridge=bridge,
        delivery_key=f"{session.id}:slack:on_output:test",
        method="on_output",
        payload={"text": "retry me", "metadata": {"final": True}},
        event_seq=1,
    )

    assert sent is False
    pending = fresh_store.pending_bridge_deliveries(session.id, "slack")
    assert len(pending) == 1
    assert "slack send failed" in (pending[0].last_error or "")

    client.fail = False
    await subscriber._drain_pending_deliveries(session.id, "slack", bridge)

    assert fresh_store.pending_bridge_deliveries(session.id, "slack") == []
    assert client.messages[-1]["text"] == "retry me"
