"""Per-session bridge output accumulation."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha1
from typing import Any


@dataclass(frozen=True)
class BridgeFlush:
    """Output ready to send to a chat bridge."""

    text: str
    metadata: dict[str, Any] | None = None
    final_key: str | None = None


@dataclass
class _TurnState:
    """Mutable output state for one bridge session turn."""

    stream_parts: list[str] = field(default_factory=list)
    bridge_segments: list[dict[str, str]] = field(default_factory=list)
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
        state.stream_parts.clear()
        state.bridge_segments.clear()

    def buffer_stream(
        self,
        session_id: str,
        text: str,
        bridge_segments: list[dict[str, str]] | None = None,
    ) -> None:
        """Buffer streamed output until it is safe to send."""

        state = self._state(session_id)
        state.stream_parts.append(text)
        if bridge_segments:
            state.bridge_segments.extend(bridge_segments)

    def buffered_size(self, session_id: str) -> int:
        """Return the total buffered stream character count."""

        state = self._states.get(session_id)
        if not state:
            return 0
        return sum(len(text) for text in state.stream_parts)

    def flush_stream(self, session_id: str) -> BridgeFlush | None:
        """Return buffered stream output and clear the stream buffer."""

        state = self._states.get(session_id)
        if not state:
            return None
        parts = state.stream_parts
        segments = state.bridge_segments
        state.stream_parts = []
        state.bridge_segments = []
        if not parts and not segments:
            return None
        text = "".join(parts)
        if not text.strip() and not segments:
            return None
        metadata = {"bridge_segments": segments} if segments else None
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
        state.stream_parts.clear()
        state.bridge_segments.clear()
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
        return sha1(normalized.encode("utf-8")).hexdigest()
