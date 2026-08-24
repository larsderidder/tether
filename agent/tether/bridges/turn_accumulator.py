"""Per-session bridge output accumulation."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any


TOOL_SEGMENT_KINDS = {
    "tool_call",
    "tool_output",
    "tool_result",
    "tool_error",
    "result",
    "error",
}


@dataclass(frozen=True)
class BridgeFlush:
    """Output ready to send to a chat bridge."""

    text: str
    metadata: dict[str, Any] | None = None
    final_key: str | None = None


@dataclass
class _StreamItem:
    """One streamed output event buffered for bridge delivery."""

    text: str
    bridge_segments: list[dict[str, str]] = field(default_factory=list)


@dataclass
class _TurnState:
    """Mutable output state for one bridge session turn."""

    stream_items: list[_StreamItem] = field(default_factory=list)
    final_sent: bool = False
    final_key: str | None = None


class BridgeTurnAccumulator:
    """Keep streamed bridge output separate from final assistant output."""

    def __init__(self) -> None:
        self._states: dict[str, _TurnState] = {}

    def reset_turn(self, session_id: str) -> None:
        """Start a fresh bridge turn for a session."""

        self._states[session_id] = _TurnState()

    def discard(self, session_id: str) -> None:
        """Drop buffered stream output while preserving final dedupe state."""

        state = self._state(session_id)
        state.stream_items.clear()

    def buffer_stream(
        self,
        session_id: str,
        text: str,
        bridge_segments: list[dict[str, str]] | None = None,
    ) -> None:
        """Buffer streamed output until it is safe to send."""

        state = self._state(session_id)
        state.stream_items.append(_StreamItem(text, list(bridge_segments or [])))

    def buffered_size(self, session_id: str) -> int:
        """Return the total buffered stream character count."""

        state = self._states.get(session_id)
        if not state:
            return 0
        return sum(len(item.text) for item in state.stream_items)

    def buffered_segment_kinds(self, session_id: str) -> set[str]:
        """Return structured segment kinds currently buffered for a session."""

        state = self._states.get(session_id)
        if not state:
            return set()
        return {
            str(segment.get("kind") or "")
            for item in state.stream_items
            for segment in item.bridge_segments
            if segment.get("kind")
        }

    def has_tool_activity(self, session_id: str) -> bool:
        """Return true when there is buffered tool telemetry for a session."""

        return bool(self.buffered_segment_kinds(session_id) & TOOL_SEGMENT_KINDS)

    def flush_tool_activity(self, session_id: str) -> BridgeFlush | None:
        """Return buffered tool telemetry as one bridge message."""

        state = self._states.get(session_id)
        if not state:
            return None

        tool_segments: list[dict[str, str]] = []
        tool_text_parts: list[str] = []
        remaining: list[_StreamItem] = []

        for item in state.stream_items:
            item_tool_segments = [
                segment
                for segment in item.bridge_segments
                if str(segment.get("kind") or "") in TOOL_SEGMENT_KINDS
            ]
            item_other_segments = [
                segment
                for segment in item.bridge_segments
                if str(segment.get("kind") or "") not in TOOL_SEGMENT_KINDS
            ]

            if item_tool_segments:
                tool_segments.extend(item_tool_segments)
                tool_text_parts.append(item.text)

            if item_other_segments:
                remaining.append(_StreamItem(item.text, item_other_segments))
            elif not item.bridge_segments and item.text:
                remaining.append(item)

        state.stream_items = remaining
        if not tool_segments:
            return None

        text = "".join(tool_text_parts).strip() or "Tool activity"
        return BridgeFlush(
            text=text,
            metadata={"bridge_segments": tool_segments, "tool_activity": True},
        )

    def flush_stream(self, session_id: str) -> BridgeFlush | None:
        """Return buffered stream output and clear the stream buffer."""

        state = self._states.get(session_id)
        if not state:
            return None
        items = state.stream_items
        state.stream_items = []
        if not items:
            return None

        text = "".join(item.text for item in items)
        segments = [segment for item in items for segment in item.bridge_segments]
        if not text.strip() and not segments:
            return None
        metadata = (
            {"bridge_segments": segments, "stream_batch": True} if segments else None
        )
        return BridgeFlush(text=text, metadata=metadata)

    def final_output(
        self,
        session_id: str,
        text: str,
        metadata: dict[str, Any],
        *,
        turn_id: str | None = None,
    ) -> BridgeFlush | None:
        """Return exactly one final assistant output per bridge turn."""

        state = self._state(session_id)
        state.stream_items.clear()
        if not text.strip():
            return None

        key = turn_id or self._text_key(text)
        if state.final_sent:
            return None
        final_metadata = dict(metadata)
        if turn_id:
            final_metadata["turn_id"] = turn_id
        return BridgeFlush(text=text, metadata=final_metadata, final_key=key)

    def mark_final_sent(self, session_id: str, final_key: str | None) -> None:
        """Record that the current turn has delivered final output."""

        state = self._state(session_id)
        state.final_sent = True
        state.final_key = final_key

    def remove(self, session_id: str) -> None:
        """Remove all accumulator state for a session."""

        self._states.pop(session_id, None)

    def _state(self, session_id: str) -> _TurnState:
        return self._states.setdefault(session_id, _TurnState())

    @staticmethod
    def _text_key(text: str) -> str:
        normalized = " ".join(text.split())
        return sha256(normalized.encode("utf-8")).hexdigest()
