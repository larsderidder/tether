"""Tests for Discord bridge (Phase 5 PoC)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

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
    async def test_on_output_formats_tool_messages_for_discord(
        self, fresh_store: SessionStore
    ) -> None:
        """Tool calls and tool output get distinct Discord styling."""
        from tether.bridges.discord.bot import DiscordBridge

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "discord"
        session.platform_thread_id = "9876543210"
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

        await bridge.on_output(session.id, "[tool: bash]\n[bash] pwd\n/tmp/demo")

        sent_messages = [call.args[0] for call in mock_thread.send.await_args_list]
        assert sent_messages[0] == "🔧 **Tool call** `bash`"
        assert sent_messages[1].startswith("📥 **Tool output** `bash`\n```text\n")
        assert "/tmp/demo" in sent_messages[1]

    @pytest.mark.anyio
    async def test_on_output_restores_thread_id_from_persisted_session(
        self, fresh_store: SessionStore
    ) -> None:
        """Persisted thread bindings should recover after bridge state drift."""
        from tether.bridges.discord.bot import DiscordBridge

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "discord"
        session.platform_thread_id = "9876543210"
        fresh_store.update_session(session)

        mock_client = MagicMock()
        mock_thread = AsyncMock()
        mock_client.get_channel.return_value = mock_thread

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client

        await bridge.on_output(session.id, "Recovered Discord output")

        assert bridge._thread_ids[session.id] == 9876543210
        assert mock_thread.send.called

    @pytest.mark.anyio
    async def test_sync_force_passes_force_flag_to_callback(
        self, fresh_store: SessionStore
    ) -> None:
        """!sync force should request a force sync from the backend."""
        from tether.bridges.discord.bot import DiscordBridge

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "discord"
        session.platform_thread_id = "9876543210"
        fresh_store.update_session(session)

        sync_session = AsyncMock(return_value={"synced": 9, "total": 9})
        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
            callbacks=_mock_callbacks(sync_session=sync_session),
        )
        bridge._thread_ids[session.id] = 9876543210

        class FakeThread:
            def __init__(self) -> None:
                self.id = 9876543210
                self.send = AsyncMock()

        class FakeDiscord:
            Thread = FakeThread

        thread = FakeThread()
        message = MagicMock()
        message.channel = thread
        message.author.id = 1
        message.content = "!sync force"

        with patch.dict("sys.modules", {"discord": FakeDiscord}):
            await bridge._cmd_sync(message)

        sync_session.assert_awaited_once_with(session.id, force=True)
        thread.send.assert_awaited_once_with("🔄 Force-synced 9 message(s) (9 total).")

    @pytest.mark.anyio
    async def test_on_output_uploads_requested_attachments(
        self, fresh_store: SessionStore, tmp_path
    ) -> None:
        """Final output attachments are uploaded into the same Discord thread."""
        from tether.bridges.discord.bot import DiscordBridge

        report = tmp_path / "report.md"
        report.write_text("hello", encoding="utf-8")

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "discord"
        session.platform_thread_id = "9876543210"
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

        fake_discord = MagicMock()
        fake_discord.File.side_effect = lambda path, filename=None, description=None: {
            "path": path,
            "filename": filename,
            "description": description,
        }

        with patch.dict("sys.modules", {"discord": fake_discord}):
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

        assert mock_thread.send.await_count >= 2
        assert mock_thread.send.await_args_list[-1].kwargs["files"][0]["path"] == str(
            report
        )

    @pytest.mark.anyio
    async def test_on_output_final_updates_starter_status_bar(
        self, fresh_store: SessionStore
    ) -> None:
        """Final responses refresh the visible control-channel starter message."""
        from tether.bridges.discord.bot import DiscordBridge

        session = fresh_store.create_session("repo_test", "main")

        mock_client = MagicMock()
        mock_channel = AsyncMock()
        mock_channel.id = 1234567890
        mock_starter_message = AsyncMock()
        mock_starter_message.id = 111222333
        mock_thread = AsyncMock()
        mock_thread.id = 9876543210
        mock_starter_message.create_thread.return_value = mock_thread
        mock_channel.send.return_value = mock_starter_message
        mock_client.get_channel.side_effect = lambda channel_id: {
            1234567890: mock_channel,
            9876543210: mock_thread,
        }.get(channel_id)

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client

        await bridge.create_thread(session.id, "Status Session")
        mock_starter_message.edit.reset_mock()

        await bridge.on_output(
            session.id,
            "Final Discord output",
            metadata={"final": True},
        )

        edited_text = mock_starter_message.edit.await_args.kwargs["content"]
        assert "response finished" in edited_text.lower()
        assert "Latest" in edited_text

    def test_session_for_thread_restores_mapping_from_store(
        self, fresh_store: SessionStore
    ) -> None:
        """Inbound thread replies should still resolve after restart."""
        from tether.bridges.discord.bot import DiscordBridge

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "discord"
        session.platform_thread_id = "9876543210"
        fresh_store.update_session(session)

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )

        assert bridge._session_for_thread(9876543210) == session.id
        assert bridge._thread_ids[session.id] == 9876543210

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
        mock_starter_message = AsyncMock()
        mock_thread = MagicMock()
        mock_thread.id = 9876543210
        mock_starter_message.create_thread.return_value = mock_thread
        mock_channel.send.return_value = mock_starter_message
        mock_client.get_channel.return_value = mock_channel

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client

        # Create thread
        result = await bridge.create_thread(session.id, "Test Session")

        # Verify a visible thread was created from a starter message
        assert mock_channel.send.called
        assert mock_starter_message.create_thread.called
        assert result["thread_id"] == "9876543210"
        assert result["platform"] == "discord"

    @pytest.mark.anyio
    async def test_create_thread_renders_emojiful_status_bar_starter_message(
        self, fresh_store: SessionStore
    ) -> None:
        """Visible starter messages show a live status bar instead of placeholder text."""
        from tether.bridges.discord.bot import DiscordBridge

        session = fresh_store.create_session("repo_test", "main")

        mock_client = MagicMock()
        mock_channel = AsyncMock()
        mock_channel.id = 1234567890
        mock_starter_message = AsyncMock()
        mock_starter_message.id = 111222333
        mock_thread = MagicMock()
        mock_thread.id = 9876543210
        mock_starter_message.create_thread.return_value = mock_thread
        mock_channel.send.return_value = mock_starter_message
        mock_client.get_channel.return_value = mock_channel

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client

        await bridge.create_thread(session.id, "Test Session")

        sent_text = mock_channel.send.await_args.args[0]
        assert "This starter message keeps the thread visible" not in sent_text
        assert "**Status bar:**" in sent_text
        assert "🤖 Control <#1234567890>" in sent_text

        edited_text = mock_starter_message.edit.await_args.kwargs["content"]
        assert "**Status bar:**" in edited_text
        assert "🤖 Control <#1234567890>" in edited_text
        assert "🧵 Thread <#9876543210>" in edited_text
        assert "👥 Access open" in edited_text
        assert "Thread ready" in edited_text

    @pytest.mark.anyio
    async def test_create_thread_starts_starter_refresh_task(
        self, fresh_store: SessionStore
    ) -> None:
        """Starter messages keep a refresh task running until the session is removed."""
        from tether.bridges.discord.bot import DiscordBridge

        session = fresh_store.create_session("repo_test", "main")

        mock_client = MagicMock()
        mock_channel = AsyncMock()
        mock_channel.id = 1234567890
        mock_starter_message = AsyncMock()
        mock_starter_message.id = 111222333
        mock_thread = AsyncMock()
        mock_thread.id = 9876543210
        mock_starter_message.create_thread.return_value = mock_thread
        mock_channel.send.return_value = mock_starter_message
        mock_client.get_channel.side_effect = lambda channel_id: {
            1234567890: mock_channel,
            9876543210: mock_thread,
        }.get(channel_id)

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client

        await bridge.create_thread(session.id, "Refresh Task Session")

        assert session.id in bridge._starter_refresh_tasks
        assert not bridge._starter_refresh_tasks[session.id].done()

        await bridge.on_session_removed(session.id)

        assert session.id not in bridge._starter_refresh_tasks

    @pytest.mark.anyio
    async def test_create_thread_suppresses_pingy_mentions_in_status_bar(
        self, fresh_store: SessionStore
    ) -> None:
        """Starter status bars render channel/user mentions without notifying people."""
        from tether.bridges.discord.bot import DiscordBridge, DiscordConfig

        session = fresh_store.create_session("repo_test", "main")

        mock_client = MagicMock()
        mock_channel = AsyncMock()
        mock_channel.id = 1234567890
        mock_starter_message = AsyncMock()
        mock_starter_message.id = 111222333
        mock_thread = MagicMock()
        mock_thread.id = 9876543210
        mock_starter_message.create_thread.return_value = mock_thread
        mock_channel.send.return_value = mock_starter_message
        mock_client.get_channel.return_value = mock_channel

        fake_allowed_mentions = MagicMock()
        fake_allowed_mentions.none.return_value = "NO_PINGS"
        fake_discord = MagicMock()
        fake_discord.AllowedMentions = fake_allowed_mentions

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
            discord_config=DiscordConfig(allowed_user_ids=[111, 222]),
        )
        bridge._client = mock_client

        with patch.dict("sys.modules", {"discord": fake_discord}):
            await bridge.create_thread(session.id, "No Ping Session")

        sent_kwargs = mock_channel.send.await_args.kwargs
        edited_kwargs = mock_starter_message.edit.await_args.kwargs
        assert sent_kwargs["allowed_mentions"] == "NO_PINGS"
        assert edited_kwargs["allowed_mentions"] == "NO_PINGS"
        assert "<@111>" in edited_kwargs["content"]
        assert "<@222>" in edited_kwargs["content"]

    @pytest.mark.anyio
    async def test_create_thread_fetches_preconfigured_channel_when_cache_empty(
        self, fresh_store: SessionStore
    ) -> None:
        """Preconfigured control channels do not rely on Discord cache warmup."""
        from tether.bridges.discord.bot import DiscordBridge

        session = fresh_store.create_session("repo_test", "main")

        mock_client = MagicMock()
        fetched_channel = AsyncMock()
        fetched_channel.id = 1234567890
        mock_starter_message = AsyncMock()
        mock_thread = MagicMock()
        mock_thread.id = 222333444
        mock_starter_message.create_thread.return_value = mock_thread
        fetched_channel.send.return_value = mock_starter_message
        mock_client.get_channel.return_value = None
        mock_client.fetch_channel = AsyncMock(return_value=fetched_channel)

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client

        result = await bridge.create_thread(session.id, "Fetched Channel Session")

        mock_client.fetch_channel.assert_awaited_once_with(1234567890)
        fetched_channel.send.assert_awaited_once()
        mock_starter_message.create_thread.assert_awaited_once()
        assert result["thread_id"] == "222333444"
        assert result["platform"] == "discord"

    @pytest.mark.anyio
    async def test_rename_thread_updates_discord_thread_name(
        self, fresh_store: SessionStore, tmp_path
    ) -> None:
        """Discord thread renames edit the existing thread."""
        from agent_tether.base import BridgeConfig
        from tether.bridges.discord.bot import DiscordBridge

        mock_client = MagicMock()
        mock_channel = AsyncMock()
        mock_starter_message = AsyncMock()
        mock_thread = AsyncMock()
        mock_thread.id = 222333444
        mock_starter_message.create_thread.return_value = mock_thread
        mock_channel.send.return_value = mock_starter_message
        mock_client.get_channel.side_effect = [mock_channel, mock_thread]

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
            config=BridgeConfig(data_dir=str(tmp_path)),
        )
        bridge._client = mock_client

        await bridge.create_thread("sess_1", "Repo")
        await bridge.rename_thread("sess_1", "tether: rename thread after first input")

        mock_thread.edit.assert_awaited_once_with(
            name="tether: rename thread after first input"
        )
        assert (
            bridge._thread_names["sess_1"] == "tether: rename thread after first input"
        )

    @pytest.mark.anyio
    async def test_auto_control_channel_reuses_existing_hostname_channel(
        self, monkeypatch, tmp_path
    ) -> None:
        from tether.bridges.discord.bot import DiscordBridge, DiscordConfig
        from agent_tether.base import BridgeConfig

        monkeypatch.setattr(
            "tether.bridges.discord.bot.socket.gethostname", lambda: "box4080"
        )

        mock_client = MagicMock()
        existing_channel = MagicMock()
        existing_channel.id = 555
        existing_channel.name = "🤖-box4080"
        mock_guild = MagicMock()
        mock_guild.id = 123456
        mock_guild.text_channels = [existing_channel]
        mock_client.get_guild.return_value = mock_guild
        mock_client.guilds = [mock_guild]

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=0,
            discord_config=DiscordConfig(guild_id=123456),
            config=BridgeConfig(data_dir=str(tmp_path)),
        )
        bridge._client = mock_client

        channel = await bridge._ensure_control_channel()

        assert channel is existing_channel
        assert bridge._channel_id == 555
        assert not mock_guild.create_text_channel.called

    @pytest.mark.anyio
    async def test_auto_control_channel_creates_missing_hostname_channel(
        self, monkeypatch, tmp_path
    ) -> None:
        from tether.bridges.discord.bot import DiscordBridge, DiscordConfig
        from agent_tether.base import BridgeConfig

        monkeypatch.setattr(
            "tether.bridges.discord.bot.socket.gethostname", lambda: "kali14"
        )

        mock_client = MagicMock()
        created_channel = MagicMock()
        created_channel.id = 777
        created_channel.name = "🤖-kali14"
        mock_guild = MagicMock()
        mock_guild.id = 654321
        mock_guild.text_channels = []
        mock_guild.create_text_channel = AsyncMock(return_value=created_channel)
        mock_client.get_guild.return_value = mock_guild
        mock_client.guilds = [mock_guild]

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=0,
            discord_config=DiscordConfig(guild_id=654321),
            config=BridgeConfig(data_dir=str(tmp_path)),
        )
        bridge._client = mock_client

        channel = await bridge._ensure_control_channel()

        assert channel is created_channel
        assert bridge._channel_id == 777
        mock_guild.create_text_channel.assert_awaited_once()
        assert mock_guild.create_text_channel.await_args.kwargs["name"] == "🤖-kali14"

    @pytest.mark.anyio
    async def test_create_thread_bootstraps_control_channel_when_unset(
        self, fresh_store: SessionStore, monkeypatch, tmp_path
    ) -> None:
        from tether.bridges.discord.bot import DiscordBridge, DiscordConfig
        from agent_tether.base import BridgeConfig

        monkeypatch.setattr(
            "tether.bridges.discord.bot.socket.gethostname", lambda: "thinkpad1"
        )

        session = fresh_store.create_session("repo_test", "main")

        control_channel = AsyncMock()
        control_channel.id = 1001
        control_channel.name = "🤖-thinkpad1"
        starter_message = AsyncMock()
        thread = MagicMock()
        thread.id = 2002
        starter_message.create_thread.return_value = thread
        control_channel.send.return_value = starter_message

        mock_guild = MagicMock()
        mock_guild.id = 8080
        mock_guild.text_channels = [control_channel]

        mock_client = MagicMock()
        mock_client.get_guild.return_value = mock_guild
        mock_client.guilds = [mock_guild]

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=0,
            discord_config=DiscordConfig(guild_id=8080),
            config=BridgeConfig(data_dir=str(tmp_path)),
        )
        bridge._client = mock_client

        result = await bridge.create_thread(session.id, "Test Session")

        assert bridge._channel_id == 1001
        control_channel.send.assert_awaited_once()
        starter_message.create_thread.assert_awaited_once()
        assert result["thread_id"] == "2002"

    @pytest.mark.anyio
    async def test_thread_names_are_unique_like_telegram(
        self, fresh_store: SessionStore, tmp_path
    ) -> None:
        from tether.bridges.discord.bot import DiscordBridge
        from agent_tether.base import BridgeConfig

        # Mock Discord client
        mock_client = MagicMock()
        mock_channel = AsyncMock()
        mock_starter_message = AsyncMock()
        mock_thread = MagicMock()
        mock_thread.id = 111
        mock_starter_message.create_thread.return_value = mock_thread
        mock_channel.send.return_value = mock_starter_message
        mock_client.get_channel.return_value = mock_channel

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
            config=BridgeConfig(data_dir=str(tmp_path)),
        )
        bridge._client = mock_client

        name1 = bridge._make_external_thread_name(
            directory="/repo", session_id="sess_1"
        )
        await bridge.create_thread("sess_1", name1)

        mock_thread_2 = MagicMock()
        mock_thread_2.id = 222
        mock_starter_message_2 = AsyncMock()
        mock_starter_message_2.create_thread.return_value = mock_thread_2
        mock_channel.send.return_value = mock_starter_message_2

        name2 = bridge._make_external_thread_name(
            directory="/repo", session_id="sess_2"
        )
        await bridge.create_thread("sess_2", name2)

        assert name1 == "Repo"
        assert name2 == "Repo 2"

    @pytest.mark.anyio
    async def test_create_thread_falls_back_for_non_text_channels(
        self, fresh_store: SessionStore
    ) -> None:
        """Non-text Discord channels still use the upstream thread creation path."""
        from tether.bridges.discord.bot import DiscordBridge

        session = fresh_store.create_session("repo_test", "main")

        mock_client = MagicMock()
        mock_channel = object()
        mock_client.get_channel.return_value = mock_channel

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
        )
        bridge._client = mock_client

        with patch(
            "agent_tether.discord.bot.DiscordBridge.create_thread",
            new=AsyncMock(return_value={"thread_id": "321", "platform": "discord"}),
        ) as create_thread:
            result = await bridge.create_thread(session.id, "Fallback Session")

        assert create_thread.await_count == 1
        assert result["thread_id"] == "321"

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
        from agent_tether.base import BridgeConfig

        monkeypatch.setenv("TETHER_DEBUG_ATTACH_LOGS", "0")

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "discord"
        fresh_store.update_session(session)

        mock_client = MagicMock()
        mock_thread = AsyncMock()
        mock_client.get_channel.return_value = mock_thread

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
            config=BridgeConfig(error_debounce_seconds=30),
        )
        bridge._client = mock_client
        bridge._thread_ids[session.id] = 9876543210

        import agent_tether.base as base_mod

        t = 1000.0
        monkeypatch.setattr(base_mod.time, "time", lambda: t)
        await bridge.on_status_change(session.id, "error")
        t += 1.0
        await bridge.on_status_change(session.id, "error")

        # Only the first error should be sent within debounce window.
        assert mock_thread.send.call_count == 1

    @pytest.mark.anyio
    async def test_error_status_uploads_debug_attachments_when_enabled(
        self, fresh_store: SessionStore, monkeypatch
    ) -> None:
        """Error status uploads Discord file attachments when enabled."""
        from tether.bridges.discord.bot import DiscordBridge

        monkeypatch.setenv("TETHER_DEBUG_ATTACH_LOGS", "1")

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "discord"
        session.platform_thread_id = "9876543210"
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

        await bridge.on_status_change(
            session.id,
            "error",
            metadata={"message": "Process crashed"},
        )

        assert mock_thread.send.called
        kwargs = mock_thread.send.call_args.kwargs
        assert "files" in kwargs
        assert kwargs["files"]
        assert "Process crashed" in mock_thread.send.call_args.args[0]

    @pytest.mark.anyio
    async def test_error_status_falls_back_to_plain_status_when_disabled(
        self, fresh_store: SessionStore, monkeypatch
    ) -> None:
        """Disabling debug attachments restores the plain error status message."""
        from tether.bridges.discord.bot import DiscordBridge

        monkeypatch.setenv("TETHER_DEBUG_ATTACH_LOGS", "0")

        session = fresh_store.create_session("repo_test", "main")
        session.platform = "discord"
        session.platform_thread_id = "9876543210"
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

        await bridge.on_status_change(
            session.id,
            "error",
            metadata={"message": "Process crashed"},
        )

        assert mock_thread.send.called
        assert "files" not in mock_thread.send.call_args.kwargs

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

        # Flush the buffered auto-approve notification
        items = bridge._auto_approve_buffer.pop(session.id, [])
        task = bridge._auto_approve_flush_tasks.pop(session.id, None)
        if task:
            task.cancel()
        if items:
            await bridge.send_auto_approve_batch(session.id, items)

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
        callbacks = _mock_callbacks()

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
            callbacks=callbacks,
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

        await bridge._forward_input(mock_message, session.id, "deny: bad approach")

        # Should have called respond_to_permission
        callbacks.respond_to_permission.assert_called_once()
        args = callbacks.respond_to_permission.call_args[0]
        assert args[0] == session.id
        assert args[1] == "req_456"
        assert args[2] is False
        assert "bad approach" in args[3]

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
        callbacks = _mock_callbacks()

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
            callbacks=callbacks,
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

        await bridge._forward_input(mock_message, session.id, "fix the bug please")

        # Should have called send_input, not respond_to_permission
        callbacks.send_input.assert_called_once_with(session.id, "fix the bug please")
        callbacks.respond_to_permission.assert_not_called()

    @pytest.mark.anyio
    async def test_forward_input_updates_starter_message_with_user_mention(
        self, fresh_store: SessionStore
    ) -> None:
        """Forwarded input refreshes the visible starter status with a user mention."""
        from tether.bridges.discord.bot import DiscordBridge

        session = fresh_store.create_session("repo_test", "main")

        mock_client = MagicMock()
        mock_channel = AsyncMock()
        mock_channel.id = 1234567890
        mock_starter_message = AsyncMock()
        mock_starter_message.id = 111222333
        mock_thread = AsyncMock()
        mock_thread.id = 9876543210
        mock_starter_message.create_thread.return_value = mock_thread
        mock_channel.send.return_value = mock_starter_message
        mock_client.get_channel.side_effect = lambda channel_id: {
            1234567890: mock_channel,
            9876543210: mock_thread,
        }.get(channel_id)
        callbacks = _mock_callbacks()

        bridge = DiscordBridge(
            bot_token="discord_bot_token",
            channel_id=1234567890,
            callbacks=callbacks,
        )
        bridge._client = mock_client

        await bridge.create_thread(session.id, "Mention Session")
        mock_starter_message.edit.reset_mock()

        mock_message = MagicMock()
        mock_message.channel = mock_thread
        mock_message.channel.id = 9876543210
        mock_message.author.id = 4242
        mock_message.author.name = "testuser"

        await bridge._forward_input(mock_message, session.id, "ship it")

        edited_text = mock_starter_message.edit.await_args.kwargs["content"]
        assert "<@4242>" in edited_text
        assert "input queued" in edited_text.lower()

    @pytest.mark.anyio
    async def test_pairing_required_blocks_unpaired_input(
        self, fresh_store: SessionStore, monkeypatch
    ) -> None:
        from tether.bridges.discord.bot import DiscordBridge, DiscordConfig

        session = fresh_store.create_session("repo_test", "main")

        mock_client = MagicMock()
        mock_thread = AsyncMock()
        mock_client.get_channel.return_value = mock_thread

        bridge = DiscordBridge(
            bot_token="x",
            channel_id=1234567890,
            discord_config=DiscordConfig(require_pairing=True, pairing_code="12345678"),
        )
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
        from tether.bridges.discord.bot import DiscordBridge, DiscordConfig

        callbacks = _mock_callbacks()
        bridge = DiscordBridge(
            bot_token="x",
            channel_id=1234567890,
            discord_config=DiscordConfig(require_pairing=True, pairing_code="12345678"),
            callbacks=callbacks,
        )

        mock_channel = AsyncMock()
        mock_channel.id = 1234567890
        mock_message = MagicMock()
        mock_message.channel = mock_channel
        mock_message.guild = MagicMock()
        mock_message.author.id = 222
        mock_message.author.name = "testuser"

        await bridge._dispatch_command(mock_message, "!pair 12345678")
        assert 222 in bridge._paired_user_ids

        await bridge._dispatch_command(mock_message, "!status")
        callbacks.list_sessions.assert_called_once()

    @pytest.mark.anyio
    async def test_auto_pair_user_ids_authorize_commands(self, tmp_path) -> None:
        from agent_tether.base import BridgeConfig
        from tether.bridges.discord.bot import DiscordBridge, DiscordConfig

        callbacks = _mock_callbacks()
        bridge = DiscordBridge(
            bot_token="x",
            channel_id=1234567890,
            discord_config=DiscordConfig(
                require_pairing=True,
                auto_pair_user_ids=[222],
            ),
            callbacks=callbacks,
            config=BridgeConfig(data_dir=str(tmp_path)),
        )

        mock_channel = AsyncMock()
        mock_channel.id = 1234567890
        mock_message = MagicMock()
        mock_message.channel = mock_channel
        mock_message.guild = MagicMock()
        mock_message.author.id = 222
        mock_message.author.name = "testuser"

        await bridge._dispatch_command(mock_message, "!status")

        assert 222 in bridge._paired_user_ids
        callbacks.list_sessions.assert_called_once()
        pairing_payload = json.loads(
            (tmp_path / "discord_pairing.json").read_text("utf-8")
        )
        assert pairing_payload["paired_user_ids"] == [222]

    @pytest.mark.anyio
    async def test_setup_command_sets_control_channel_and_pairs_user(
        self, fresh_store: SessionStore, monkeypatch
    ) -> None:
        from tether.bridges.discord.bot import DiscordBridge, DiscordConfig

        bridge = DiscordBridge(
            bot_token="x",
            channel_id=0,
            discord_config=DiscordConfig(pairing_code="12345678"),
        )

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

    @pytest.mark.anyio
    async def test_typing_indicator_starts_and_stops(
        self, fresh_store: SessionStore
    ) -> None:
        """on_typing starts a typing indicator loop, on_typing_stopped cancels it."""
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

        # Start typing indicator
        await bridge.on_typing(session.id)
        assert session.id in bridge._typing_tasks
        typing_task = bridge._typing_tasks[session.id]
        assert not typing_task.done()

        # Stop typing indicator
        await bridge.on_typing_stopped(session.id)
        assert session.id not in bridge._typing_tasks

        # Give the task a moment to cancel
        import asyncio

        await asyncio.sleep(0.01)
        assert typing_task.cancelled() or typing_task.done()

    @pytest.mark.anyio
    async def test_typing_indicator_calls_discord_api(
        self, fresh_store: SessionStore
    ) -> None:
        """Typing indicator loop calls thread.typing()."""
        import asyncio

        from tether.bridges.discord.bot import DiscordBridge

        # Create session with Discord binding
        session = fresh_store.create_session("repo_test", "main")

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

        # Start typing indicator
        await bridge.on_typing(session.id)

        # Wait a bit to let the typing loop run at least once
        await asyncio.sleep(0.1)

        # Stop typing indicator
        await bridge.on_typing_stopped(session.id)

        # Verify typing() was called
        assert mock_thread.typing.called
