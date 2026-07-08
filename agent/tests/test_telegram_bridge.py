"""Tests for Telegram bridge integration."""

from importlib.util import find_spec
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tether.bridges.base import ApprovalRequest
from tether.store import SessionStore

HAS_TELEGRAM = find_spec("telegram") is not None


class TestTelegramBridgeIntegration:
    """Test Telegram bridge implementation following BridgeInterface."""

    @pytest.mark.anyio
    async def test_telegram_bridge_implements_interface(self) -> None:
        """TelegramBridge implements BridgeInterface correctly."""
        from tether.bridges.telegram.bot import TelegramBridge
        from tether.bridges.base import BridgeInterface

        # Verify it's a subclass
        assert issubclass(TelegramBridge, BridgeInterface)

    @pytest.mark.anyio
    async def test_telegram_bridge_can_be_instantiated(self) -> None:
        """TelegramBridge can be created with minimal config."""
        from tether.bridges.telegram.bot import TelegramBridge

        # Should be able to create with token and group ID
        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
        )
        assert bridge is not None

    @pytest.mark.anyio
    async def test_rename_thread_updates_telegram_topic(self) -> None:
        """rename_thread updates the Telegram forum topic name."""
        from tether.bridges.telegram.bot import TelegramBridge

        mock_app = MagicMock()
        mock_bot = AsyncMock()
        mock_app.bot = mock_bot

        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
        )
        bridge._app = mock_app
        bridge._state.set_topic_for_session("sess_1", 12345, "Old name")

        renamed = await bridge.rename_thread("sess_1", "New pi session")

        assert renamed == "New pi session"
        mock_bot.edit_forum_topic.assert_awaited_once_with(
            chat_id=-1001234567890,
            message_thread_id=12345,
            name="New pi session",
        )
        assert bridge._state._mappings["sess_1"].name == "New pi session"

    @pytest.mark.anyio
    async def test_on_output_sends_to_telegram(self, fresh_store: SessionStore) -> None:
        """on_output sends text to Telegram topic."""
        from tether.bridges.telegram.bot import TelegramBridge

        # Create session with platform binding
        session = fresh_store.create_session("repo_test", "main")
        session.platform = "telegram"
        session.platform_thread_id = "12345"
        fresh_store.update_session(session)

        # Mock telegram bot
        mock_app = MagicMock()
        mock_bot = AsyncMock()
        mock_app.bot = mock_bot

        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
        )
        bridge._app = mock_app

        # Record topic mapping
        bridge._state.set_topic_for_session(session.id, 12345, "Test")

        # Send output
        await bridge.on_output(session.id, "Test output message")

        # Verify bot sent message
        assert mock_bot.send_message.called

    @pytest.mark.anyio
    async def test_on_output_sends_large_message_batches_slowly(
        self, fresh_store: SessionStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """on_output sends every Telegram chunk unless a cap is configured."""
        from tether.bridges.telegram.bot import TelegramBridge

        monkeypatch.setattr(
            "tether.bridges.telegram.bot._TELEGRAM_OUTPUT_MIN_INTERVAL_S", 0
        )
        monkeypatch.delenv("TETHER_TELEGRAM_OUTPUT_MAX_MESSAGES", raising=False)
        session = fresh_store.create_session("repo_test", "main")
        session.platform = "telegram"
        session.platform_thread_id = "12345"
        fresh_store.update_session(session)

        mock_app = MagicMock()
        mock_bot = AsyncMock()
        mock_app.bot = mock_bot

        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
        )
        bridge._app = mock_app
        bridge._state.set_topic_for_session(session.id, 12345, "Test")

        await bridge.on_output(session.id, "x" * 30000)

        assert mock_bot.send_message.await_count > 5

    @pytest.mark.anyio
    async def test_configured_output_cap_preserves_final_chunk(
        self, fresh_store: SessionStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Configured Telegram output caps keep the tail visible."""
        from tether.bridges.telegram.bot import TelegramBridge

        monkeypatch.setattr(
            "tether.bridges.telegram.bot._TELEGRAM_OUTPUT_MIN_INTERVAL_S", 0
        )
        monkeypatch.setenv("TETHER_TELEGRAM_OUTPUT_MAX_MESSAGES", "5")
        session = fresh_store.create_session("repo_test", "main")
        session.platform = "telegram"
        session.platform_thread_id = "12345"
        fresh_store.update_session(session)

        mock_app = MagicMock()
        mock_bot = AsyncMock()
        mock_app.bot = mock_bot

        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
        )
        bridge._app = mock_app
        bridge._state.set_topic_for_session(session.id, 12345, "Test")

        await bridge.on_output(session.id, "x" * 30000 + "final-tail")

        assert mock_bot.send_message.await_count == 5
        notice_text = mock_bot.send_message.await_args_list[-2].kwargs["text"]
        last_text = mock_bot.send_message.await_args_list[-1].kwargs["text"]
        assert "Telegram output shortened" in notice_text
        assert "final-tail" in last_text

    @pytest.mark.anyio
    async def test_output_rate_limit_pauses_and_recovers(
        self, fresh_store: SessionStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telegram RetryAfter pauses output and reports recovery later."""
        from tether.bridges.telegram.bot import TelegramBridge

        monkeypatch.setattr(
            "tether.bridges.telegram.bot._TELEGRAM_OUTPUT_MIN_INTERVAL_S", 0
        )
        session = fresh_store.create_session("repo_test", "main")

        mock_app = MagicMock()
        mock_bot = AsyncMock()
        mock_app.bot = mock_bot

        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
        )
        bridge._app = mock_app
        bridge._state.set_topic_for_session(session.id, 12345, "Test")

        class RetryAfterError(Exception):
            retry_after = 0.0

        async def fail_retry(*args, **kwargs):
            raise RetryAfterError()

        monkeypatch.setattr(
            "tether.bridges.telegram.bot.with_bridge_send_retry", fail_retry
        )

        sent = await bridge._send_output_message(session.id, 12345, "First")

        assert sent is False
        assert bridge._dropped_output_count_by_topic[12345] == 1
        assert bridge._output_paused_until > 0

        async def ok_retry(label, send, **kwargs):
            return await send()

        bridge._output_paused_until = 0
        monkeypatch.setattr(
            "tether.bridges.telegram.bot.with_bridge_send_retry", ok_retry
        )

        sent = await bridge._send_output_message(session.id, 12345, "Second")

        assert sent is True
        sent_text = mock_bot.send_message.await_args.kwargs["text"]
        assert "Telegram flood control recovered" in sent_text
        assert "Second" in sent_text

    @pytest.mark.anyio
    async def test_missing_topic_clears_telegram_binding(
        self, fresh_store: SessionStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleted Telegram topics detach the stale session binding."""
        from tether.bridges.telegram.bot import TelegramBridge

        monkeypatch.setattr(
            "tether.bridges.telegram.bot._TELEGRAM_OUTPUT_MIN_INTERVAL_S", 0
        )
        session = fresh_store.create_session("repo_test", "main")
        session.platform = "telegram"
        session.platform_thread_id = "12345"
        fresh_store.update_session(session)

        mock_app = MagicMock()
        mock_app.bot = AsyncMock()

        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
        )
        bridge._app = mock_app
        bridge._state.set_topic_for_session(session.id, 12345, "Test")

        async def missing_thread(label, send, **kwargs):
            raise Exception("Message thread not found")

        monkeypatch.setattr(
            "tether.bridges.telegram.bot.with_bridge_send_retry",
            missing_thread,
        )

        sent = await bridge._send_output_message(session.id, 12345, "First")

        updated = fresh_store.get_session(session.id)
        assert sent is False
        assert bridge._state.get_topic_for_session(session.id) is None
        assert updated is not None
        assert updated.platform is None
        assert updated.platform_thread_id is None

    @pytest.mark.anyio
    async def test_on_output_formats_tool_messages_for_telegram(
        self, fresh_store: SessionStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tool calls and tool output get distinct Telegram styling."""
        from tether.bridges.telegram.bot import TelegramBridge

        monkeypatch.setattr(
            "tether.bridges.telegram.bot._TELEGRAM_OUTPUT_MIN_INTERVAL_S", 0
        )
        session = fresh_store.create_session("repo_test", "main")
        session.platform = "telegram"
        session.platform_thread_id = "12345"
        fresh_store.update_session(session)

        mock_app = MagicMock()
        mock_bot = AsyncMock()
        mock_app.bot = mock_bot

        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
        )
        bridge._app = mock_app
        bridge._state.set_topic_for_session(session.id, 12345, "Test")

        await bridge.on_output(session.id, "[tool: bash]\n[bash] pwd\n/tmp/demo")

        first_text = mock_bot.send_message.await_args_list[0].kwargs["text"]
        second_text = mock_bot.send_message.await_args_list[1].kwargs["text"]
        assert first_text == "🔧 <b>Tool call</b> <code>bash</code>"
        assert second_text.startswith("📥 <b>Tool output</b> <code>bash</code>\n<pre>")
        assert "/tmp/demo" in second_text

    @pytest.mark.skipif(not HAS_TELEGRAM, reason="telegram library not installed")
    @pytest.mark.anyio
    async def test_on_approval_request_creates_inline_keyboard(
        self, fresh_store: SessionStore
    ) -> None:
        """on_approval_request creates Telegram inline keyboard."""
        from tether.bridges.telegram.bot import TelegramBridge

        # Create session
        session = fresh_store.create_session("repo_test", "main")
        session.platform = "telegram"
        session.platform_thread_id = "12345"
        fresh_store.update_session(session)

        # Mock telegram bot
        mock_app = MagicMock()
        mock_bot = AsyncMock()
        mock_app.bot = mock_bot

        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
        )
        bridge._app = mock_app
        bridge._state.set_topic_for_session(session.id, 12345, "Test")

        # Create approval request
        request = ApprovalRequest(
            request_id="req_123",
            title="Allow file write?",
            description="Write to config.yaml",
            options=["Allow", "Deny"],
            timeout_s=300,
        )

        # Mock the telegram imports that happen inside the method
        with (
            patch("telegram.InlineKeyboardButton"),
            patch("telegram.InlineKeyboardMarkup"),
        ):

            # Send approval
            await bridge.on_approval_request(session.id, request)

            # Verify message with keyboard was sent
            assert mock_bot.send_message.called
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert "reply_markup" in call_kwargs

    @pytest.mark.skipif(not HAS_TELEGRAM, reason="telegram library not installed")
    @pytest.mark.anyio
    async def test_start_registers_local_telegram_command_menu(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telegram menu includes locally added commands."""
        from tether.bridges.telegram.bot import TelegramBridge

        async def fake_upstream_start(bridge) -> None:
            bridge._app = MagicMock()
            bridge._app.bot = AsyncMock()

        monkeypatch.setattr(
            "agent_tether.telegram.bot.TelegramBridge.start",
            fake_upstream_start,
        )
        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
        )

        await bridge.start()

        commands = bridge._app.bot.set_my_commands.await_args.args[0]
        command_names = [command.command for command in commands]
        assert "sync" in command_names
        assert "compact" in command_names
        assert "diff" in command_names
        assert "log" in command_names
        assert "commit" not in command_names
        assert "push" not in command_names

    @pytest.mark.anyio
    async def test_sync_command_uses_bridge_callback(
        self, fresh_store: SessionStore
    ) -> None:
        """Telegram /sync pulls new messages through the shared callback."""
        from agent_tether.base import BridgeCallbacks
        from tether.bridges.telegram.bot import TelegramBridge

        session = fresh_store.create_session("repo_test", "main")
        callbacks = BridgeCallbacks(
            create_session=AsyncMock(),
            send_input=AsyncMock(),
            stop_session=AsyncMock(),
            respond_to_permission=AsyncMock(),
            list_sessions=AsyncMock(),
            get_usage=AsyncMock(),
            check_directory=AsyncMock(),
            list_external_sessions=AsyncMock(),
            get_external_history=AsyncMock(),
            attach_external=AsyncMock(),
            sync_session=AsyncMock(return_value={"synced": 2, "total": 5}),
        )
        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
            callbacks=callbacks,
        )
        bridge._state.set_topic_for_session(session.id, 12345, "Test")

        message = MagicMock()
        message.message_thread_id = 12345
        message.reply_text = AsyncMock()
        update = MagicMock()
        update.message = message

        await bridge._cmd_sync(update, MagicMock())

        callbacks.sync_session.assert_awaited_once_with(session.id)
        message.reply_text.assert_awaited_once_with(
            "🔄 Synced 2 new message(s) (5 total)."
        )

    @pytest.mark.skipif(not HAS_TELEGRAM, reason="telegram library not installed")
    @pytest.mark.anyio
    async def test_choice_approval_uses_short_callback_data(
        self, fresh_store: SessionStore
    ) -> None:
        """Pi extension prompts use callback data below Telegram's 64-byte limit."""
        from tether.bridges.telegram.bot import TelegramBridge

        session = fresh_store.create_session("repo_test", "main")
        mock_app = MagicMock()
        mock_bot = AsyncMock()
        mock_app.bot = mock_bot

        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
        )
        bridge._app = mock_app
        bridge._state.set_topic_for_session(session.id, 12345, "Test")

        buttons = []

        def make_button(text, callback_data):
            button = MagicMock()
            button.text = text
            button.callback_data = callback_data
            buttons.append(button)
            return button

        request = ApprovalRequest(
            kind="choice",
            request_id="pi_extui:confirm:96d62e1d-3470-4c6d-9864-5af2d5f13e49",
            title="Remember in project memory?",
            description="Save this fact?",
            options=["Yes", "No"],
        )

        with (
            patch("telegram.InlineKeyboardButton", side_effect=make_button),
            patch("telegram.InlineKeyboardMarkup"),
        ):
            await bridge.on_approval_request(session.id, request)

        assert mock_bot.send_message.called
        assert [button.text for button in buttons] == ["1. Yes", "2. No"]
        assert all(len(button.callback_data.encode()) <= 64 for button in buttons)
        assert all(button.callback_data.startswith("choice:") for button in buttons)

    @pytest.mark.anyio
    async def test_create_thread_creates_telegram_topic(
        self, fresh_store: SessionStore
    ) -> None:
        """create_thread creates a Telegram forum topic."""
        from tether.bridges.telegram.bot import TelegramBridge

        session = fresh_store.create_session("repo_test", "main")

        # Mock telegram bot
        mock_app = MagicMock()
        mock_bot = AsyncMock()
        mock_topic = MagicMock()
        mock_topic.message_thread_id = 67890
        mock_bot.create_forum_topic.return_value = mock_topic
        mock_app.bot = mock_bot

        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
        )
        bridge._app = mock_app

        # Create thread
        result = await bridge.create_thread(session.id, "Test Session")

        # Verify topic was created
        assert mock_bot.create_forum_topic.called
        assert result["thread_id"] == "67890"
        assert result["platform"] == "telegram"


class TestTelegramStateManagement:
    """Test Telegram state persistence."""

    def test_state_manager_stores_mappings(self, tmp_path) -> None:
        """StateManager persists session-to-topic mappings."""
        from tether.bridges.telegram.state import StateManager

        state_file = tmp_path / "telegram_state.json"
        manager = StateManager(str(state_file))

        # Set mapping
        manager.set_topic_for_session("sess_123", 12345, "Test Session")

        # Verify it's stored
        assert manager.get_topic_for_session("sess_123") == 12345
        assert manager.get_session_for_topic(12345) == "sess_123"

        # Verify persistence
        assert state_file.exists()

        # Load in new manager
        manager2 = StateManager(str(state_file))
        manager2.load()

        assert manager2.get_topic_for_session("sess_123") == 12345


class TestTelegramMessageFormatting:
    """Test Telegram markdown formatting."""

    @pytest.mark.anyio
    async def test_markdown_escaping(self) -> None:
        """Telegram messages escape MarkdownV2 special characters."""
        from tether.bridges.telegram.formatting import escape_markdown

        text = "Test_with*special[chars](and.more!)"
        escaped = escape_markdown(text)

        # All special chars should be escaped
        assert "\\_" in escaped
        assert "\\*" in escaped
        assert "\\[" in escaped
        assert "\\(" in escaped
        assert "\\." in escaped
        assert "\\!" in escaped

    @pytest.mark.anyio
    async def test_message_chunking(self) -> None:
        """Long messages are split at Telegram's 4096 char limit."""
        from tether.bridges.telegram.formatting import chunk_message

        long_text = "x" * 5000
        chunks = chunk_message(long_text)

        assert len(chunks) == 2
        assert len(chunks[0]) <= 4096
        assert len(chunks[1]) <= 4096
        assert "".join(chunks) == long_text
