"""Runner adapter that uses LiteLLM for OpenAI-compatible model providers."""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog

from tether.prompts import SYSTEM_PROMPT
from tether.runner.api_runner_base import ApiRunnerBase
from tether.runner.base import RunnerEvents
from tether.settings import settings
from tether.store import store
from tether.tools import TOOLS_OPENAI

logger = structlog.get_logger(__name__)


class LiteLLMRunner(ApiRunnerBase):
    """Runner that uses LiteLLM to talk to any OpenAI-compatible provider.

    Supports DeepSeek, Kimi, Gemini, OpenRouter, and 100+ other models.
    API keys are read from standard env vars by LiteLLM (e.g. OPENROUTER_API_KEY,
    DEEPSEEK_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, etc.).
    """

    runner_type: str = "litellm"

    def __init__(self, events: RunnerEvents) -> None:
        super().__init__(events)
        self._model = settings.litellm_model()
        self._max_tokens = settings.litellm_max_tokens()

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    async def _emit_header(self, session_id: str) -> None:
        await self._events.on_header(
            session_id,
            title=self._model,
            model=self._model,
            provider="LiteLLM",
        )

    async def _call_api(
        self, session_id: str, messages: list[dict]
    ) -> dict | None:
        import litellm

        try:
            # Convert Anthropic-format messages to OpenAI format
            oai_messages = self._to_openai_messages(messages)

            # Prepend system message
            oai_messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})

            content_blocks: list[dict] = []
            usage: dict[str, int] = {}
            stop_reason = None
            current_text = ""
            tool_calls_by_index: dict[int, dict] = {}

            response = await litellm.acompletion(
                model=self._model,
                messages=oai_messages,
                tools=TOOLS_OPENAI,
                stream=True,
                max_tokens=self._max_tokens,
            )

            async for chunk in response:
                if store.is_stop_requested(session_id):
                    return None

                choice = chunk.choices[0] if chunk.choices else None
                if not choice:
                    continue

                delta = choice.delta

                # Text content
                if delta and delta.content:
                    current_text += delta.content
                    await self._events.on_output(
                        session_id,
                        "combined",
                        delta.content,
                        kind="final",
                        is_final=True,
                    )

                # Tool calls (streamed incrementally)
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index if tc.index is not None else 0
                        if idx not in tool_calls_by_index:
                            tool_calls_by_index[idx] = {
                                "id": tc.id or "",
                                "name": "",
                                "arguments": "",
                            }
                        entry = tool_calls_by_index[idx]
                        if tc.id:
                            entry["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                entry["name"] = tc.function.name
                            if tc.function.arguments:
                                entry["arguments"] += tc.function.arguments

                # Finish reason
                if choice.finish_reason:
                    stop_reason = self._map_finish_reason(choice.finish_reason)

                # Usage from chunk (some providers include it)
                if hasattr(chunk, "usage") and chunk.usage:
                    if hasattr(chunk.usage, "prompt_tokens"):
                        usage["input_tokens"] = chunk.usage.prompt_tokens or 0
                    if hasattr(chunk.usage, "completion_tokens"):
                        usage["output_tokens"] = chunk.usage.completion_tokens or 0

            # Finalize text block
            if current_text:
                content_blocks.append({"type": "text", "text": current_text})

            # Finalize tool calls
            for _idx, tc in sorted(tool_calls_by_index.items()):
                try:
                    tool_input = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    logger.warning(
                        "Failed to parse tool arguments",
                        session_id=session_id,
                        arguments=tc["arguments"][:200],
                    )
                    tool_input = {}

                content_blocks.append({
                    "type": "tool_use",
                    "id": tc["id"] or f"call_{uuid.uuid4().hex[:12]}",
                    "name": tc["name"],
                    "input": tool_input,
                })

            # If we got tool calls, the stop reason should reflect that
            if tool_calls_by_index and stop_reason != "end_turn":
                stop_reason = "tool_use"

            return {
                "content": content_blocks,
                "stop_reason": stop_reason or "end_turn",
                "usage": usage,
            }

        except Exception:
            logger.exception("LiteLLM API call failed", session_id=session_id)
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
        # Store in Anthropic format (same as Claude runner) — conversion
        # to OpenAI format happens in _to_openai_messages
        tool_results = [
            {
                "type": "tool_result",
                "tool_use_id": r["tool_use"]["id"],
                "content": r["content"],
            }
            for r in results
        ]
        store.add_message(session_id, "user", tool_results)

    # ------------------------------------------------------------------
    # Message format conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _to_openai_messages(messages: list[dict]) -> list[dict]:
        """Convert Anthropic-format message history to OpenAI format.

        Anthropic format:
            {"role": "user", "content": [{"type": "text", "text": "..."}]}
            {"role": "assistant", "content": [{"type": "text"}, {"type": "tool_use"}]}
            {"role": "user", "content": [{"type": "tool_result", ...}]}

        OpenAI format:
            {"role": "user", "content": "..."}
            {"role": "assistant", "content": "...", "tool_calls": [...]}
            {"role": "tool", "tool_call_id": "...", "content": "..."}
        """
        oai: list[dict] = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", [])

            if role == "user":
                # Check if this is tool results or regular user text
                if content and isinstance(content, list):
                    if any(b.get("type") == "tool_result" for b in content):
                        # Tool results → separate "tool" role messages
                        for block in content:
                            if block.get("type") == "tool_result":
                                oai.append({
                                    "role": "tool",
                                    "tool_call_id": block.get("tool_use_id", ""),
                                    "content": block.get("content", ""),
                                })
                        continue

                # Regular user message
                text = _extract_text(content)
                if text:
                    oai.append({"role": "user", "content": text})

            elif role == "assistant":
                text_parts: list[str] = []
                tool_calls: list[dict] = []

                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_calls.append({
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": json.dumps(
                                        block.get("input", {})
                                    ),
                                },
                            })

                entry: dict[str, Any] = {"role": "assistant"}
                combined_text = "".join(text_parts)
                if combined_text:
                    entry["content"] = combined_text
                else:
                    entry["content"] = None
                if tool_calls:
                    entry["tool_calls"] = tool_calls

                oai.append(entry)

        return oai

    @staticmethod
    def _map_finish_reason(reason: str) -> str:
        """Map OpenAI finish_reason to our internal stop_reason."""
        mapping = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "content_filter": "end_turn",
        }
        return mapping.get(reason, "end_turn")


def _extract_text(content: list | str) -> str:
    """Pull plain text from Anthropic-format content blocks."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)
