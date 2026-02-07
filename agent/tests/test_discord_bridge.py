"""Tests for Discord bridge (Phase 5 PoC)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tether.bridges.base import BridgeInterface
from tether.store import SessionStore


class TestDiscordBridgePoC:
    """Test Discord bridge PoC implementation."""

    def test_discord_bridge_implements_interface(self) -> None:
        """DiscordBridge implements BridgeInterface."""
        from tether.bridges.discord.bot import DiscordBridge

        assert issubclass(DiscordBridge, BridgeInterface)

    def test_discord_bridge_can_be_instantiated(self) -> None:
        """DiscordBridge can be created with bot token and channel."""
        from tether.bridges.discord.bot import DiscordBridge

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        assert bridge is not None

    @pytest.mark.anyio
    async def test_on_output_sends_to_discord_thread(
        self, fresh_store: SessionStore
    ) -> None:
        """on_output sends text to Discord thread."""
        from tether.bridges.discord.bot import DiscordBridge

        # Create session with Discord binding
        session = fresh_store.create_session("repo_test", "main")
        session.platform = "discord"
        session.platform_thread_id = "9876543210"
        fresh_store.update_session(session)

        # Mock Discord client
        mock_client = MagicMock()
        mock_thread = AsyncMock()
        mock_client.get_channel.return_value = mock_thread

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client
        bridge._thread_ids[session.id] = 9876543210  # Register thread

        # Send output
        await bridge.on_output(session.id, "Test Discord output")

        # Verify message was sent to Discord thread
        assert mock_thread.send.called

    @pytest.mark.anyio
    async def test_create_thread_creates_discord_thread(
        self, fresh_store: SessionStore
    ) -> None:
        """create_thread creates a Discord thread."""
        from tether.bridges.discord.bot import DiscordBridge

        session = fresh_store.create_session("repo_test", "main")

        # Mock Discord client
        mock_client = MagicMock()
        mock_channel = AsyncMock()
        mock_thread = MagicMock()
        mock_thread.id = 9876543210
        mock_channel.create_thread.return_value = mock_thread
        mock_client.get_channel.return_value = mock_channel

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client

        # Create thread
        result = await bridge.create_thread(session.id, "Test Session")

        # Verify thread was created
        assert mock_channel.create_thread.called
        assert result["thread_id"] == "9876543210"
        assert result["platform"] == "discord"

    @pytest.mark.anyio
    async def test_thread_names_are_unique_like_telegram(
        self, fresh_store: SessionStore
    ) -> None:
        from tether.bridges.discord.bot import DiscordBridge

        # Mock Discord client
        mock_client = MagicMock()
        mock_channel = AsyncMock()
        mock_thread = MagicMock()
        mock_thread.id = 111
        mock_channel.create_thread.return_value = mock_thread
        mock_client.get_channel.return_value = mock_channel

        bridge = DiscordBridge(bot_token="discord_bot_token", channel_id=1234567890)
        bridge._client = mock_client

        name1 = bridge._make_external_thread_name(directory="/repo", session_id="sess_1")
        await bridge.create_thread("sess_1", name1)

        mock_thread_2 = MagicMock()
        mock_thread_2.id = 222
        mock_channel.create_thread.return_value = mock_thread_2

        name2 = bridge._make_external_thread_name(directory="/repo", session_id="sess_2")
        await bridge.create_thread("sess_2", name2)

        assert name1 == "Repo"
        assert name2 == "Repo 2"

    @pytest.mark.anyio
    async def test_on_status_change_sends_to_discord(
        self, fresh_store: SessionStore
    ) -> None:
        """on_status_change sends status to Discord thread."""
        from tether.bridges.discord.bot import DiscordBridge

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "discord"
        session.platform_thread_id = "9876543210"
        fresh_store.update_session(session)

        # Mock Discord client
        mock_client = MagicMock()
        mock_thread = AsyncMock()
        mock_client.get_channel.return_value = mock_thread

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client
        bridge._thread_ids[session.id] = 9876543210  # Register thread

        # Send status
        await bridge.on_status_change(session.id, "executing")

        # Verify status was sent
        assert mock_thread.send.called

    @pytest.mark.anyio
    async def test_on_status_change_error_is_debounced(
        self, fresh_store: SessionStore, monkeypatch
    ) -> None:
        """Repeated error status changes shouldn't spam."""
        from tether.bridges.discord.bot import DiscordBridge

        monkeypatch.setenv("TETHER_AGENT_BRIDGE_ERROR_DEBOUNCE_SECONDS", "30")

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "discord"
        fresh_store.update_session(session)

        mock_client = MagicMock()
        mock_thread = AsyncMock()
        mock_client.get_channel.return_value = mock_thread

        bridge = DiscordBridge(bot_token="discord_bot_token", channel_id=1234567890)
        bridge._client = mock_client
        bridge._thread_ids[session.id] = 9876543210

        import tether.bridges.base as base_mod

        t = 1000.0
        monkeypatch.setattr(base_mod.time, "time", lambda: t)
        await bridge.on_status_change(session.id, "error")
        t += 1.0
        await bridge.on_status_change(session.id, "error")

        # Only the first error should be sent within debounce window.
        assert mock_thread.send.call_count == 1

    @pytest.mark.anyio
    async def test_on_approval_request_sends_message(
        self, fresh_store: SessionStore
    ) -> None:
        """Approval requests send message to Discord thread."""
        from tether.bridges.discord.bot import DiscordBridge
        from tether.bridges.base import ApprovalRequest

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "discord"
        fresh_store.update_session(session)

        mock_client = MagicMock()
        mock_thread = AsyncMock()
        mock_client.get_channel.return_value = mock_thread

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client
        bridge._thread_ids[session.id] = 9876543210

        request = ApprovalRequest(
            request_id="req_123",
            title="Read",
            description="Read config.yaml",
            options=["Allow", "Deny"],
        )

        await bridge.on_approval_request(session.id, request)

        assert mock_thread.send.called
        sent_text = mock_thread.send.call_args.args[0]
        assert "Approval Required" in sent_text
        assert "deny: <reason>" in sent_text
        # Should track pending permission
        assert bridge.get_pending_permission(session.id) is request

    @pytest.mark.anyio
    async def test_on_approval_request_auto_approves(
        self, fresh_store: SessionStore
    ) -> None:
        """Approval requests auto-approve when allow-all timer is active."""
        from tether.bridges.discord.bot import DiscordBridge
        from tether.bridges.base import ApprovalRequest

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "discord"
        fresh_store.update_session(session)

        mock_client = MagicMock()
        mock_thread = AsyncMock()
        mock_client.get_channel.return_value = mock_thread

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client
        bridge._thread_ids[session.id] = 9876543210
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
        assert mock_thread.send.called
        sent_text = mock_thread.send.call_args.args[0]
        assert "auto-approved" in sent_text
        assert "Approval Required" not in sent_text

    @pytest.mark.anyio
    async def test_forward_input_deny_with_reason(
        self, fresh_store: SessionStore
    ) -> None:
        """Typing 'deny: reason' in thread resolves permission."""
        from tether.bridges.discord.bot import DiscordBridge
        from tether.bridges.base import ApprovalRequest

        session = fresh_store.create_session("repo_test", "main")

        mock_client = MagicMock()
        mock_thread = AsyncMock()
        mock_client.get_channel.return_value = mock_thread

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client
        bridge._thread_ids[session.id] = 9876543210

        request = ApprovalRequest(
            request_id="req_456",
            title="Write",
            description="Write file",
            options=["Allow", "Deny"],
        )
        bridge.set_pending_permission(session.id, request)

        mock_message = MagicMock()
        mock_message.channel = mock_thread
        mock_message.channel.id = 9876543210
        mock_message.author.name = "testuser"

        with patch("httpx.AsyncClient") as mock_http:
            mock_http_inst = AsyncMock()
            mock_http_inst.__aenter__ = AsyncMock(return_value=mock_http_inst)
            mock_http_inst.__aexit__ = AsyncMock(return_value=False)
            mock_http_inst.post.return_value = MagicMock(
                status_code=200, raise_for_status=MagicMock()
            )
            mock_http.return_value = mock_http_inst

            await bridge._forward_input(mock_message, session.id, "deny: bad approach")

            # Should have called permission API
            mock_http_inst.post.assert_called_once()
            call_args = mock_http_inst.post.call_args
            assert "/permission" in call_args[0][0]
            body = call_args[1]["json"]
            assert body["allow"] is False
            assert "bad approach" in body["message"]

        # Should have sent confirmation
        assert mock_thread.send.called

    @pytest.mark.anyio
    async def test_forward_input_non_approval_passes_through(
        self, fresh_store: SessionStore
    ) -> None:
        """Regular text with pending permission still forwards as input."""
        from tether.bridges.discord.bot import DiscordBridge
        from tether.bridges.base import ApprovalRequest

        session = fresh_store.create_session("repo_test", "main")
        session.state = "RUNNING"
        fresh_store.update_session(session)

        mock_client = MagicMock()
        mock_thread = AsyncMock()
        mock_client.get_channel.return_value = mock_thread

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client
        bridge._thread_ids[session.id] = 9876543210

        request = ApprovalRequest(
            request_id="req_abc",
            title="Read",
            description="Read file",
            options=["Allow", "Deny"],
        )
        bridge.set_pending_permission(session.id, request)

        mock_message = MagicMock()
        mock_message.channel = mock_thread
        mock_message.channel.id = 9876543210
        mock_message.author.name = "testuser"

        with patch("httpx.AsyncClient") as mock_http:
            mock_http_inst = AsyncMock()
            mock_http_inst.__aenter__ = AsyncMock(return_value=mock_http_inst)
            mock_http_inst.__aexit__ = AsyncMock(return_value=False)
            mock_http_inst.post.return_value = MagicMock(
                status_code=200, raise_for_status=MagicMock()
            )
            mock_http.return_value = mock_http_inst

            await bridge._forward_input(mock_message, session.id, "fix the bug please")

            # Should have called input API, not permission API
            call_url = mock_http_inst.post.call_args[0][0]
            assert "/input" in call_url or "/start" in call_url

    @pytest.mark.anyio
    async def test_pairing_required_blocks_unpaired_input(
        self, fresh_store: SessionStore, monkeypatch
    ) -> None:
        from tether.bridges.discord.bot import DiscordBridge

        monkeypatch.setenv("DISCORD_REQUIRE_PAIRING", "1")
        monkeypatch.setenv("DISCORD_PAIRING_CODE", "12345678")

        session = fresh_store.create_session("repo_test", "main")

        mock_client = MagicMock()
        mock_thread = AsyncMock()
        mock_client.get_channel.return_value = mock_thread

        bridge = DiscordBridge(bot_token="x", channel_id=1234567890)
        bridge._client = mock_client
        bridge._thread_ids[session.id] = 9876543210

        mock_message = MagicMock()
        mock_message.channel = mock_thread
        mock_message.channel.id = 9876543210
        mock_message.author.name = "testuser"
        mock_message.author.id = 111

        with patch("httpx.AsyncClient") as mock_http:
            mock_http_inst = AsyncMock()
            mock_http_inst.__aenter__ = AsyncMock(return_value=mock_http_inst)
            mock_http_inst.__aexit__ = AsyncMock(return_value=False)
            mock_http.return_value = mock_http_inst

            await bridge._forward_input(mock_message, session.id, "hello")

            # Should NOT have sent anything to the API (blocked on pairing)
            assert not mock_http_inst.post.called

        assert mock_thread.send.called

    @pytest.mark.anyio
    async def test_pair_command_pairs_user_and_allows_commands(
        self, fresh_store: SessionStore, monkeypatch
    ) -> None:
        from tether.bridges.discord.bot import DiscordBridge

        monkeypatch.setenv("DISCORD_REQUIRE_PAIRING", "1")
        monkeypatch.setenv("DISCORD_PAIRING_CODE", "12345678")

        bridge = DiscordBridge(bot_token="x", channel_id=1234567890)

        mock_channel = AsyncMock()
        mock_channel.id = 1234567890
        mock_message = MagicMock()
        mock_message.channel = mock_channel
        mock_message.guild = MagicMock()
        mock_message.author.id = 222
        mock_message.author.name = "testuser"

        await bridge._dispatch_command(mock_message, "!pair 12345678")
        assert 222 in bridge._paired_user_ids

        with patch("httpx.AsyncClient") as mock_http:
            mock_http_inst = AsyncMock()
            mock_http_inst.__aenter__ = AsyncMock(return_value=mock_http_inst)
            mock_http_inst.__aexit__ = AsyncMock(return_value=False)
            mock_http_inst.get.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value=[]),
                raise_for_status=MagicMock(),
            )
            mock_http.return_value = mock_http_inst

            await bridge._dispatch_command(mock_message, "!status")
            assert mock_http_inst.get.called

    @pytest.mark.anyio
    async def test_setup_command_sets_control_channel_and_pairs_user(
        self, fresh_store: SessionStore, monkeypatch
    ) -> None:
        from tether.bridges.discord.bot import DiscordBridge

        monkeypatch.setenv("DISCORD_PAIRING_CODE", "12345678")

        bridge = DiscordBridge(bot_token="x", channel_id=0)

        mock_channel = AsyncMock()
        mock_channel.id = 999
        mock_message = MagicMock()
        mock_message.channel = mock_channel
        mock_message.guild = MagicMock()
        mock_message.author.id = 333
        mock_message.author.name = "testuser"

        await bridge._dispatch_command(mock_message, "!setup 12345678")
        assert bridge._channel_id == 999
        assert 333 in bridge._paired_user_ids
