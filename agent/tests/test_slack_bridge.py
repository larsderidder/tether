"""Tests for Slack bridge (Phase 4 PoC)."""

from unittest.mock import AsyncMock, patch

import pytest

from agent_tether.base import BridgeCallbacks
from tether.bridges.base import BridgeInterface
from tether.store import SessionStore


def _mock_callbacks(**overrides) -> BridgeCallbacks:
    """Create BridgeCallbacks with all methods mocked."""
    defaults = dict(
        create_session=AsyncMock(return_value={}),
        send_input=AsyncMock(),
        stop_session=AsyncMock(),
        respond_to_permission=AsyncMock(return_value=True),
        list_sessions=AsyncMock(return_value=[]),
        get_usage=AsyncMock(return_value={}),
        check_directory=AsyncMock(return_value={"exists": True, "path": "/tmp"}),
        list_external_sessions=AsyncMock(return_value=[]),
        get_external_history=AsyncMock(return_value=None),
        attach_external=AsyncMock(return_value={}),
    )
    defaults.update(overrides)
    return BridgeCallbacks(**defaults)


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
        self, fresh_store: SessionStore, tmp_path
    ) -> None:
        """Second thread with same directory gets 'Name 2'."""
        from agent_tether.base import BridgeConfig
        from tether.bridges.slack.bot import SlackBridge

        mock_client = AsyncMock()
        mock_client.chat_postMessage.side_effect = [
            {"ok": True, "ts": "1"},
            {"ok": True, "ts": "2"},
        ]

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
            config=BridgeConfig(data_dir=str(tmp_path)),
        )
        bridge._client = mock_client

        name1 = bridge._make_external_thread_name(
            directory="/repo", session_id="sess_1"
        )
        await bridge.create_thread("sess_1", name1)

        name2 = bridge._make_external_thread_name(
            directory="/repo", session_id="sess_2"
        )
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
    async def test_on_output_formats_tool_messages_for_slack(
        self, fresh_store: SessionStore
    ) -> None:
        """Tool calls and tool output get distinct Slack styling."""
        from tether.bridges.slack.bot import SlackBridge

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "slack"
        session.platform_thread_id = "1234567890.123456"
        fresh_store.update_session(session)

        mock_client = AsyncMock()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
        )
        bridge._client = mock_client
        bridge._thread_ts[session.id] = "1234567890.123456"

        await bridge.on_output(session.id, "[tool: bash]\n[bash] pwd\n/tmp/demo")

        first_text = mock_client.chat_postMessage.await_args_list[0].kwargs["text"]
        second_text = mock_client.chat_postMessage.await_args_list[1].kwargs["text"]
        assert first_text == "🔧 **Tool call** `bash`"
        assert second_text.startswith("📥 **Tool output** `bash`\n```text\n")
        assert "/tmp/demo" in second_text

    @pytest.mark.anyio
    async def test_sync_force_passes_force_flag_to_callback(
        self, fresh_store: SessionStore
    ) -> None:
        """!sync force should request a force sync from the backend."""
        from tether.bridges.slack.bot import SlackBridge

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "slack"
        session.platform_thread_id = "1234567890.123456"
        fresh_store.update_session(session)

        sync_session = AsyncMock(return_value={"synced": 9, "total": 9})
        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
            callbacks=_mock_callbacks(sync_session=sync_session),
        )
        bridge._thread_ts[session.id] = "1234567890.123456"
        bridge._reply = AsyncMock()

        event = {
            "thread_ts": "1234567890.123456",
            "text": "!sync force",
        }

        await bridge._cmd_sync(event)

        sync_session.assert_awaited_once_with(session.id, force=True)
        bridge._reply.assert_awaited_once_with(
            event,
            "🔄 Force-synced 9 message(s) (9 total).",
        )

    @pytest.mark.anyio
    async def test_on_output_uploads_requested_attachments(
        self, fresh_store: SessionStore, tmp_path
    ) -> None:
        """Final output attachments are uploaded into the same Slack thread."""
        from tether.bridges.slack.bot import SlackBridge

        report = tmp_path / "report.md"
        report.write_text("hello", encoding="utf-8")

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "slack"
        session.platform_thread_id = "1234567890.123456"
        fresh_store.update_session(session)

        mock_client = AsyncMock()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
        )
        bridge._client = mock_client
        bridge._thread_ts[session.id] = "1234567890.123456"

        await bridge.on_output(
            session.id,
            "Final report\nSTOP 🛑✅ 2s",
            metadata={
                "final": True,
                "attachments": [
                    {
                        "path": str(report),
                        "filename": "report.md",
                        "title": "report.md",
                    }
                ],
            },
        )

        assert mock_client.chat_postMessage.called
        assert mock_client.files_upload_v2.called
        assert mock_client.files_upload_v2.await_args.kwargs["file"] == str(report)

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
    async def test_rename_thread_updates_slack_parent_message(
        self, fresh_store: SessionStore, tmp_path
    ) -> None:
        """Slack thread renames update the parent message text."""
        from agent_tether.base import BridgeConfig
        from tether.bridges.slack.bot import SlackBridge

        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "1234567890.123456",
        }

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
            config=BridgeConfig(data_dir=str(tmp_path)),
        )
        bridge._client = mock_client

        await bridge.create_thread("sess_1", "Repo")
        await bridge.rename_thread("sess_1", "tether: rename thread after first input")

        mock_client.chat_update.assert_awaited_once_with(
            channel="C01234567",
            ts="1234567890.123456",
            text="*Session:* tether: rename thread after first input",
        )
        assert (
            bridge._thread_names["sess_1"] == "tether: rename thread after first input"
        )

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
    async def test_error_status_uploads_debug_attachments_when_enabled(
        self, fresh_store: SessionStore, monkeypatch
    ) -> None:
        """Error status uploads attachment snippets instead of plain text."""
        from tether.bridges.slack.bot import SlackBridge

        monkeypatch.setenv("TETHER_DEBUG_ATTACH_LOGS", "1")

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "slack"
        session.platform_thread_id = "1234567890.123456"
        fresh_store.update_session(session)

        mock_client = AsyncMock()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
        )
        bridge._client = mock_client
        bridge._thread_ts[session.id] = "1234567890.123456"

        await bridge.on_status_change(
            session.id,
            "error",
            metadata={"message": "Process crashed"},
        )

        assert mock_client.files_upload_v2.called
        kwargs = mock_client.files_upload_v2.call_args_list[0].kwargs
        assert kwargs["thread_ts"] == "1234567890.123456"
        assert "initial_comment" in kwargs
        assert "Process crashed" in kwargs["initial_comment"]

    @pytest.mark.anyio
    async def test_error_status_falls_back_to_plain_status_when_disabled(
        self, fresh_store: SessionStore, monkeypatch
    ) -> None:
        """Disabling debug attachments restores the plain error status message."""
        from tether.bridges.slack.bot import SlackBridge

        monkeypatch.setenv("TETHER_DEBUG_ATTACH_LOGS", "0")

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "slack"
        session.platform_thread_id = "1234567890.123456"
        fresh_store.update_session(session)

        mock_client = AsyncMock()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
        )
        bridge._client = mock_client
        bridge._thread_ts[session.id] = "1234567890.123456"

        await bridge.on_status_change(
            session.id,
            "error",
            metadata={"message": "Process crashed"},
        )

        assert mock_client.chat_postMessage.called
        assert not mock_client.files_upload_v2.called

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
        callbacks = _mock_callbacks()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
            callbacks=callbacks,
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

        await bridge._forward_input(
            event, session.id, "deny: use cookies instead of JWT"
        )

        # Should have called respond_to_permission, not send_input
        callbacks.respond_to_permission.assert_called_once()
        args = callbacks.respond_to_permission.call_args[0]
        assert args[0] == session.id
        assert args[1] == "req_456"
        assert args[2] is False
        assert "use cookies instead of JWT" in args[3]
        callbacks.send_input.assert_not_called()

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
        callbacks = _mock_callbacks()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
            callbacks=callbacks,
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

        await bridge._forward_input(event, session.id, "allow")

        callbacks.respond_to_permission.assert_called_once()
        args = callbacks.respond_to_permission.call_args[0]
        assert args[2] is True  # allow=True

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
        callbacks = _mock_callbacks()

        bridge = SlackBridge(
            bot_token="xoxb-test-token",
            channel_id="C01234567",
            callbacks=callbacks,
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

        await bridge._forward_input(event, session.id, "fix the bug please")

        # Should have called send_input, not respond_to_permission
        callbacks.send_input.assert_called_once_with(session.id, "fix the bug please")
        callbacks.respond_to_permission.assert_not_called()

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

        # Flush the buffered auto-approve notification
        items = bridge._auto_approve_buffer.pop(session.id, [])
        task = bridge._auto_approve_flush_tasks.pop(session.id, None)
        if task:
            task.cancel()
        if items:
            await bridge.send_auto_approve_batch(session.id, items)

        # Should have sent a short notification (not the full approval prompt)
        assert mock_client.chat_postMessage.called
        sent_text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "auto-approved" in sent_text
        assert "Approval Required" not in sent_text
