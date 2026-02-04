"""Tests for Telegram bridge integration."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tether.bridges.base import ApprovalRequest
from tether.models import SessionState
from tether.store import SessionStore

# Check if telegram is installed
try:
    import telegram
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False


class TestTelegramBridgeIntegration:
    """Test Telegram bridge implementation following BridgeInterface."""

    @pytest.mark.asyncio
    async def test_telegram_bridge_implements_interface(self) -> None:
        """TelegramBridge implements BridgeInterface correctly."""
        from tether.bridges.telegram.bot import TelegramBridge
        from tether.bridges.base import BridgeInterface

        # Verify it's a subclass
        assert issubclass(TelegramBridge, BridgeInterface)

    @pytest.mark.asyncio
    async def test_telegram_bridge_can_be_instantiated(self) -> None:
        """TelegramBridge can be created with minimal config."""
        from tether.bridges.telegram.bot import TelegramBridge

        # Should be able to create with token and group ID
        bridge = TelegramBridge(
            bot_token="test_token",
            forum_group_id=-1001234567890,
        )
        assert bridge is not None

    @pytest.mark.asyncio
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

    @pytest.mark.skipif(not HAS_TELEGRAM, reason="telegram library not installed")
    @pytest.mark.asyncio
    async def test_on_approval_request_creates_inline_keyboard(self, fresh_store: SessionStore) -> None:
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
        with patch("telegram.InlineKeyboardButton") as mock_button, \
             patch("telegram.InlineKeyboardMarkup") as mock_markup:

            # Send approval
            await bridge.on_approval_request(session.id, request)

            # Verify message with keyboard was sent
            assert mock_bot.send_message.called
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert "reply_markup" in call_kwargs

    @pytest.mark.asyncio
    async def test_create_thread_creates_telegram_topic(self, fresh_store: SessionStore) -> None:
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
    async def test_message_chunking(self) -> None:
        """Long messages are split at Telegram's 4096 char limit."""
        from tether.bridges.telegram.formatting import chunk_message

        long_text = "x" * 5000
        chunks = chunk_message(long_text)

        assert len(chunks) == 2
        assert len(chunks[0]) <= 4096
        assert len(chunks[1]) <= 4096
        assert "".join(chunks) == long_text
