"""Runner adapter that uses the Anthropic Python SDK directly."""

from __future__ import annotations

from typing import Any

import structlog
from anthropic import Anthropic

from tether.prompts import SYSTEM_PROMPT
from tether.runner.api_runner_base import ApiRunnerBase
from tether.runner.base import RunnerEvents
from tether.settings import settings
from tether.store import store
from tether.tools import TOOLS

logger = structlog.get_logger(__name__)


class ClaudeRunner(ApiRunnerBase):
    """Runner that uses the Anthropic Python SDK directly."""

    runner_type: str = "claude_api"

    def __init__(self, events: RunnerEvents) -> None:
        super().__init__(events)
        self._client = Anthropic()
        self._model = settings.claude_model()
        self._max_tokens = settings.claude_max_tokens()

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    async def _emit_header(self, session_id: str) -> None:
        await self._events.on_header(
            session_id,
            title="Claude API",
            model=self._model,
            provider="Anthropic",
        )

    async def _call_api(
        self, session_id: str, messages: list[dict]
    ) -> dict | None:
        try:
            content_blocks: list[dict] = []
            current_text = ""
            usage: dict[str, int] = {}
            stop_reason = None

            with self._client.messages.stream(
                model=self._model,
                max_tokens=self._max_tokens,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=TOOLS,
            ) as stream:
                for event in stream:
                    if store.is_stop_requested(session_id):
                        return None

                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "text":
                            current_text = ""
                        elif block.type == "tool_use":
                            content_blocks.append({
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": {},
                            })

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            current_text += delta.text
                            await self._events.on_output(
                                session_id,
                                "combined",
                                delta.text,
                                kind="final",
                                is_final=True,
                            )

                    elif event.type == "content_block_stop":
                        if current_text:
                            content_blocks.append({
                                "type": "text",
                                "text": current_text,
                            })
                            current_text = ""

                    elif event.type == "message_delta":
                        if hasattr(event, "delta"):
                            stop_reason = getattr(event.delta, "stop_reason", None)
                        if hasattr(event, "usage"):
                            usage["output_tokens"] = getattr(
                                event.usage, "output_tokens", 0
                            )

                    elif event.type == "message_start":
                        if hasattr(event, "message") and hasattr(
                            event.message, "usage"
                        ):
                            usage["input_tokens"] = getattr(
                                event.message.usage, "input_tokens", 0
                            )

                # Replace with final parsed versions for complete tool inputs
                final_message = stream.get_final_message()
                if final_message:
                    content_blocks = []
                    for block in final_message.content:
                        if block.type == "text":
                            content_blocks.append({
                                "type": "text",
                                "text": block.text,
                            })
                        elif block.type == "tool_use":
                            content_blocks.append({
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            })
                    stop_reason = final_message.stop_reason

            return {
                "content": content_blocks,
                "stop_reason": stop_reason,
                "usage": usage,
            }

        except Exception:
            logger.exception("API call failed", session_id=session_id)
            raise

    def _add_user_message(self, session_id: str, text: str) -> None:
        store.add_message(session_id, "user", [{"type": "text", "text": text}])

    def _save_assistant_response(
        self, session_id: str, content_blocks: list[dict]
    ) -> None:
        store.add_message(session_id, "assistant", content_blocks)

    def _extract_tool_uses(self, content_blocks: list[dict]) -> list[dict]:
        return [b for b in content_blocks if b.get("type") == "tool_use"]

    def _add_tool_results(
        self,
        session_id: str,
        tool_uses: list[dict],
        results: list[dict[str, Any]],
    ) -> None:
        tool_results = [
            {
                "type": "tool_result",
                "tool_use_id": r["tool_use"]["id"],
                "content": r["content"],
            }
            for r in results
        ]
        store.add_message(session_id, "user", tool_results)
