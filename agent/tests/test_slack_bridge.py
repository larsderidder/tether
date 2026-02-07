"""Tests for Slack bridge (Phase 4 PoC)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tether.bridges.base import BridgeInterface
from tether.store import SessionStore


class TestSlackBridgePoC:
    """Test Slack bridge PoC implementation."""

    def test_slack_bridge_implements_interface(self) -> None:
        """SlackBridge implements BridgeInterface."""
        from tether.bridges.slack.bot import SlackBridge

        assert issubclass(SlackBridge, BridgeInterface)

    def test_slack_bridge_can_be_instantiated(self) -> None:
        """SlackBridge can be created with bot token and channel."""
        from tether.bridges.slack.bot import SlackBridge

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
        )
        assert bridge is not None

    @pytest.mark.anyio
    async def test_thread_names_are_unique_like_telegram(
        self, fresh_store: SessionStore
    ) -> None:
        """Second thread with same directory gets 'Name 2'."""
        from tether.bridges.slack.bot import SlackBridge

        mock_client = AsyncMock()
        mock_client.chat_postMessage.side_effect = [
            {"ok": True, "ts": "1"},
            {"ok": True, "ts": "2"},
        ]

        bridge = SlackBridge(bot_token="xoxb-test-token", channel_id="C01234567")
        bridge._client = mock_client

        name1 = bridge._make_external_thread_name(directory="/repo", session_id="sess_1")
        await bridge.create_thread("sess_1", name1)

        name2 = bridge._make_external_thread_name(directory="/repo", session_id="sess_2")
        await bridge.create_thread("sess_2", name2)

        assert name1 == "Repo"
        assert name2 == "Repo 2"

    @pytest.mark.anyio
    async def test_on_output_sends_to_slack_thread(
        self, fresh_store: SessionStore
    ) -> None:
        """on_output sends text to Slack thread."""
        from tether.bridges.slack.bot import SlackBridge

        # Create session with Slack binding
        session = fresh_store.create_session("repo_test", "main")
        session.platform = "slack"
        session.platform_thread_id = "1234567890.123456"
        fresh_store.update_session(session)

        # Mock Slack client
        mock_client = AsyncMock()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
        )
        bridge._client = mock_client
        bridge._thread_ts[session.id] = "1234567890.123456"  # Register thread

        # Send output
        await bridge.on_output(session.id, "Test Slack output")

        # Verify message was sent to Slack thread
        assert mock_client.chat_postMessage.called

    @pytest.mark.anyio
    async def test_create_thread_creates_slack_thread(
        self, fresh_store: SessionStore
    ) -> None:
        """create_thread creates a Slack thread."""
        from tether.bridges.slack.bot import SlackBridge

        session = fresh_store.create_session("repo_test", "main")

        # Mock Slack client
        mock_client = AsyncMock()
        mock_response = {"ts": "1234567890.123456", "ok": True}
        mock_client.chat_postMessage.return_value = mock_response

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
        )
        bridge._client = mock_client

        # Create thread
        result = await bridge.create_thread(session.id, "Test Session")

        # Verify thread was created
        assert mock_client.chat_postMessage.called
        assert result["thread_id"] == "1234567890.123456"
        assert result["platform"] == "slack"

    @pytest.mark.anyio
    async def test_on_status_change_sends_to_slack(
        self, fresh_store: SessionStore
    ) -> None:
        """on_status_change sends status to Slack thread."""
        from tether.bridges.slack.bot import SlackBridge

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "slack"
        session.platform_thread_id = "1234567890.123456"
        fresh_store.update_session(session)

        # Mock Slack client
        mock_client = AsyncMock()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
        )
        bridge._client = mock_client
        bridge._thread_ts[session.id] = "1234567890.123456"  # Register thread

        # Send status
        await bridge.on_status_change(session.id, "thinking")

        # Verify status was sent
        assert mock_client.chat_postMessage.called

    @pytest.mark.anyio
    async def test_on_approval_request_sends_message(
        self, fresh_store: SessionStore
    ) -> None:
        """Approval requests send message to Slack thread."""
        from tether.bridges.slack.bot import SlackBridge
        from tether.bridges.base import ApprovalRequest

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "slack"
        fresh_store.update_session(session)

        mock_client = AsyncMock()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
        )
        bridge._client = mock_client
        bridge._thread_ts[session.id] = "1234567890.123456"

        request = ApprovalRequest(
            request_id="req_123",
            title="Read",
            description="Read config.yaml",
            options=["Allow", "Deny"],
        )

        await bridge.on_approval_request(session.id, request)

        assert mock_client.chat_postMessage.called
        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert "Approval Required" in call_kwargs["text"]
        assert "deny: <reason>" in call_kwargs["text"]
        # Should track pending permission
        assert bridge.get_pending_permission(session.id) is request

    @pytest.mark.anyio
    async def test_forward_input_deny_with_reason(
        self, fresh_store: SessionStore
    ) -> None:
        """Typing 'deny: reason' in thread resolves permission, not forwards input."""
        from tether.bridges.slack.bot import SlackBridge
        from tether.bridges.base import ApprovalRequest

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "slack"
        fresh_store.update_session(session)

        mock_client = AsyncMock()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
        )
        bridge._client = mock_client
        bridge._thread_ts[session.id] = "1234567890.123456"

        request = ApprovalRequest(
            request_id="req_456",
            title="Write",
            description="Write file",
            options=["Allow", "Deny"],
        )
        bridge.set_pending_permission(session.id, request)

        event = {
            "text": "deny: use cookies instead of JWT",
            "thread_ts": "1234567890.123456",
            "user": "U123",
        }

        with patch("httpx.AsyncClient") as mock_http:
            mock_http_inst = AsyncMock()
            mock_http_inst.__aenter__ = AsyncMock(return_value=mock_http_inst)
            mock_http_inst.__aexit__ = AsyncMock(return_value=False)
            mock_http_inst.post.return_value = MagicMock(
                status_code=200, raise_for_status=MagicMock()
            )
            mock_http.return_value = mock_http_inst

            await bridge._forward_input(
                event, session.id, "deny: use cookies instead of JWT"
            )

            # Should have called permission API, not input API
            mock_http_inst.post.assert_called_once()
            call_args = mock_http_inst.post.call_args
            assert "/permission" in call_args[0][0]
            body = call_args[1]["json"]
            assert body["allow"] is False
            assert "use cookies instead of JWT" in body["message"]

        # Should have sent confirmation
        assert mock_client.chat_postMessage.called
        sent_text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "Denied" in sent_text

    @pytest.mark.anyio
    async def test_forward_input_allow_with_pending(
        self, fresh_store: SessionStore
    ) -> None:
        """Typing 'allow' in thread resolves permission."""
        from tether.bridges.slack.bot import SlackBridge
        from tether.bridges.base import ApprovalRequest

        session = fresh_store.create_session("repo_test", "main")

        mock_client = AsyncMock()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
        )
        bridge._client = mock_client
        bridge._thread_ts[session.id] = "1234567890.123456"

        request = ApprovalRequest(
            request_id="req_789",
            title="Read",
            description="Read file",
            options=["Allow", "Deny"],
        )
        bridge.set_pending_permission(session.id, request)

        event = {"text": "allow", "thread_ts": "1234567890.123456", "user": "U123"}

        with patch("httpx.AsyncClient") as mock_http:
            mock_http_inst = AsyncMock()
            mock_http_inst.__aenter__ = AsyncMock(return_value=mock_http_inst)
            mock_http_inst.__aexit__ = AsyncMock(return_value=False)
            mock_http_inst.post.return_value = MagicMock(
                status_code=200, raise_for_status=MagicMock()
            )
            mock_http.return_value = mock_http_inst

            await bridge._forward_input(event, session.id, "allow")

            body = mock_http_inst.post.call_args[1]["json"]
            assert body["allow"] is True

    @pytest.mark.anyio
    async def test_forward_input_non_approval_passes_through(
        self, fresh_store: SessionStore
    ) -> None:
        """Regular text with pending permission still forwards as input."""
        from tether.bridges.slack.bot import SlackBridge
        from tether.bridges.base import ApprovalRequest

        session = fresh_store.create_session("repo_test", "main")
        session.state = "RUNNING"
        fresh_store.update_session(session)

        mock_client = AsyncMock()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
        )
        bridge._client = mock_client
        bridge._thread_ts[session.id] = "1234567890.123456"

        request = ApprovalRequest(
            request_id="req_abc",
            title="Read",
            description="Read file",
            options=["Allow", "Deny"],
        )
        bridge.set_pending_permission(session.id, request)

        event = {
            "text": "fix the bug please",
            "thread_ts": "1234567890.123456",
            "user": "U123",
        }

        with patch("httpx.AsyncClient") as mock_http:
            mock_http_inst = AsyncMock()
            mock_http_inst.__aenter__ = AsyncMock(return_value=mock_http_inst)
            mock_http_inst.__aexit__ = AsyncMock(return_value=False)
            mock_http_inst.post.return_value = MagicMock(
                status_code=200, raise_for_status=MagicMock()
            )
            mock_http.return_value = mock_http_inst

            await bridge._forward_input(event, session.id, "fix the bug please")

            # Should have called input API, not permission API
            call_url = mock_http_inst.post.call_args[0][0]
            assert "/input" in call_url or "/start" in call_url

    @pytest.mark.anyio
    async def test_on_approval_request_auto_approves(
        self, fresh_store: SessionStore
    ) -> None:
        """Approval requests auto-approve when allow-all timer is active."""
        from tether.bridges.slack.bot import SlackBridge
        from tether.bridges.base import ApprovalRequest

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "slack"
        fresh_store.update_session(session)

        mock_client = AsyncMock()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
        )
        bridge._client = mock_client
        bridge._thread_ts[session.id] = "1234567890.123456"
        bridge.set_allow_all(session.id)

        request = ApprovalRequest(
            request_id="req_123",
            title="Read",
            description="Read config.yaml",
            options=["Allow", "Deny"],
        )

        with patch("httpx.AsyncClient") as mock_http:
            mock_http_inst = AsyncMock()
            mock_http_inst.__aenter__ = AsyncMock(return_value=mock_http_inst)
            mock_http_inst.__aexit__ = AsyncMock(return_value=False)
            mock_http.return_value = mock_http_inst

            await bridge.on_approval_request(session.id, request)

        # Should have sent a short notification (not the full approval prompt)
        assert mock_client.chat_postMessage.called
        sent_text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "auto-approved" in sent_text
        assert "Approval Required" not in sent_text
