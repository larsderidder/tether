"""Slack bridge implementation (PoC - output streaming + threading only)."""

import structlog

from tether.bridges.base import ApprovalRequest, BridgeInterface
from tether.settings import settings

logger = structlog.get_logger(__name__)


class SlackBridge(BridgeInterface):
    """Slack bridge that routes agent events to Slack threads.

    PoC scope: output streaming and threading only. Approvals deferred.

    Args:
        bot_token: Slack bot token (xoxb-...).
        channel_id: Slack channel ID.
    """

    def __init__(self, bot_token: str, channel_id: str):
        self._bot_token = bot_token
        self._channel_id = channel_id
        self._client: any = None
        self._app: any = None
        self._thread_ts: dict[str, str] = {}  # session_id -> thread_ts

    async def start(self) -> None:
        """Initialize Slack client and socket mode."""
        try:
            from slack_sdk.web.async_client import AsyncWebClient
            from slack_bolt.async_app import AsyncApp
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        except ImportError:
            logger.error("slack_sdk or slack_bolt not installed. Install with: pip install slack-sdk slack-bolt")
            return

        self._client = AsyncWebClient(token=self._bot_token)

        # Check if socket mode is available
        app_token = settings.slack_app_token()
        if app_token:
            try:
                # Initialize Bolt app for socket mode (event handling)
                self._app = AsyncApp(token=self._bot_token)

                # Register message event handler
                @self._app.event("message")
                async def handle_message(event, say):
                    await self._handle_message(event)

                # Start socket mode handler in background
                handler = AsyncSocketModeHandler(self._app, app_token)
                import asyncio
                asyncio.create_task(handler.start_async())

                logger.info("Slack bridge initialized with socket mode", channel_id=self._channel_id)
            except Exception:
                logger.exception("Failed to initialize Slack socket mode, falling back to basic mode")
                logger.info("Slack bridge initialized (basic mode, no input forwarding)", channel_id=self._channel_id)
        else:
            logger.info("Slack bridge initialized (basic mode, no input forwarding - set SLACK_APP_TOKEN for socket mode)", channel_id=self._channel_id)

    async def stop(self) -> None:
        """Stop Slack client."""
        if self._client:
            await self._client.close()
        logger.info("Slack bridge stopped")

    async def _handle_message(self, event: dict) -> None:
        """Handle incoming messages from Slack and forward to event log.

        Args:
            event: Slack message event dict.
        """
        # Ignore bot messages
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return

        # Get message text
        text = event.get("text")
        if not text:
            return

        # Check if message is in a thread
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return

        # Find session for this thread
        session_id = None
        for sid, ts in self._thread_ts.items():
            if ts == thread_ts:
                session_id = sid
                break

        if not session_id:
            logger.debug("Received message in thread with no session mapping", thread_ts=thread_ts)
            return

        # Import store here to avoid circular import
        from tether.store import store

        # Emit human_input event
        try:
            await store.emit(session_id, {
                "session_id": session_id,
                "ts": store._now(),
                "seq": store.next_seq(session_id),
                "type": "human_input",
                "data": {
                    "text": text,
                    "username": event.get("user", "unknown"),
                    "user_id": event.get("user", "unknown"),
                    "platform": "slack",
                },
            })
            logger.info(
                "Forwarded human input from Slack",
                session_id=session_id,
                thread_ts=thread_ts,
                user=event.get("user"),
            )
        except Exception:
            logger.exception(
                "Failed to forward human input",
                session_id=session_id,
                thread_ts=thread_ts,
            )

    async def on_output(
        self, session_id: str, text: str, metadata: dict | None = None
    ) -> None:
        """Send output text to Slack thread.

        Args:
            session_id: Internal Tether session ID.
            text: Output text.
            metadata: Optional metadata.
        """
        if not self._client:
            logger.warning("Slack client not initialized")
            return

        thread_ts = self._thread_ts.get(session_id)
        if not thread_ts:
            logger.warning("No Slack thread for session", session_id=session_id)
            return

        try:
            await self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text=text,
            )
        except Exception:
            logger.exception("Failed to send Slack message", session_id=session_id)

    async def on_approval_request(
        self, session_id: str, request: ApprovalRequest
    ) -> None:
        """Approval requests not implemented in Slack PoC.

        Args:
            session_id: Internal Tether session ID.
            request: Approval request (ignored).
        """
        logger.warning(
            "Approval requests not implemented in Slack PoC",
            session_id=session_id,
            request_id=request.request_id,
        )

    async def on_status_change(
        self, session_id: str, status: str, metadata: dict | None = None
    ) -> None:
        """Send status change to Slack thread.

        Args:
            session_id: Internal Tether session ID.
            status: New status.
            metadata: Optional metadata.
        """
        if not self._client:
            return

        thread_ts = self._thread_ts.get(session_id)
        if not thread_ts:
            return

        emoji_map = {
            "thinking": ":thought_balloon:",
            "executing": ":gear:",
            "done": ":white_check_mark:",
            "error": ":x:",
        }
        emoji = emoji_map.get(status, ":information_source:")

        text = f"{emoji} Status: {status}"

        try:
            await self._client.chat_postMessage(
                channel=self._channel_id,
                thread_ts=thread_ts,
                text=text,
            )
        except Exception:
            logger.exception("Failed to send Slack status", session_id=session_id)

    async def create_thread(self, session_id: str, session_name: str) -> dict:
        """Create a Slack thread for a session.

        Args:
            session_id: Internal Tether session ID.
            session_name: Display name for the session.

        Returns:
            Dict with thread_ts and platform info.
        """
        if not self._client:
            raise RuntimeError("Slack client not initialized")

        try:
            # Post initial message to create thread
            response = await self._client.chat_postMessage(
                channel=self._channel_id,
                text=f"*New Session:* {session_name}",
            )

            if not response["ok"]:
                raise RuntimeError(f"Slack API error: {response}")

            thread_ts = response["ts"]
            self._thread_ts[session_id] = thread_ts

            logger.info(
                "Created Slack thread",
                session_id=session_id,
                thread_ts=thread_ts,
                name=session_name,
            )

            return {
                "thread_id": thread_ts,
                "platform": "slack",
                "thread_ts": thread_ts,
            }

        except Exception as e:
            logger.exception("Failed to create Slack thread", session_id=session_id)
            raise RuntimeError(f"Failed to create Slack thread: {e}")
