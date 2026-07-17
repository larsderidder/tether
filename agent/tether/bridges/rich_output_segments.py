"""Parsing and coercion for bridge rich-output segments."""

from __future__ import annotations

import re

from tether.bridges.rich_output_models import OutputSegment

_RESERVED_MARKERS = {"tool", "thinking", "result", "error", "assistant"}
_MERGEABLE_SEGMENT_KINDS = {
    "assistant",
    "thinking",
    "tool_output",
    "result",
    "tool_result",
    "error",
    "tool_error",
}
_CONTINUABLE_SEGMENT_KINDS = {
    "assistant",
    "thinking",
    "tool_output",
    "result",
    "error",
    "info",
}


def coerce_output_segments(value: object) -> list[OutputSegment]:
    """Convert serialized bridge segment metadata to output segments."""

    if not isinstance(value, list):
        return []

    segments: list[OutputSegment] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if not kind:
            continue
        segment = OutputSegment(
            kind=kind,
            text=str(item.get("text") or ""),
            label=str(item["label"]) if item.get("label") else None,
        )
        if _can_merge_segments(segments[-1] if segments else None, segment):
            segments[-1].text += segment.text
        else:
            segments.append(segment)
    return segments


def _can_merge_segments(
    previous: OutputSegment | None,
    current: OutputSegment,
) -> bool:
    """Return true when adjacent streamed segments are one logical block."""

    if previous is None:
        return False
    if previous.kind != current.kind or previous.label != current.label:
        return False
    return current.kind in _MERGEABLE_SEGMENT_KINDS


def parse_output_segments(text: str) -> list[OutputSegment]:
    """Split streamed bridge text into semantically distinct chunks."""

    if not text:
        return []

    segments: list[OutputSegment] = []
    current: OutputSegment | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        current.text = current.text.rstrip("\n")
        if current.text or current.kind == "tool_call":
            segments.append(current)
        current = None

    for raw_line in text.splitlines():
        tool_call = re.fullmatch(r"\[tool:\s*([^\]]+)\]\s*", raw_line)
        if tool_call:
            flush()
            current = OutputSegment("tool_call", "", tool_call.group(1).strip())
            flush()
            continue

        thinking = re.fullmatch(r"\[thinking\]\s*(.*)", raw_line)
        if thinking:
            flush()
            current = OutputSegment("thinking", thinking.group(1), "thinking")
            continue

        assistant = re.fullmatch(r"\[assistant\]\s*(.*)", raw_line)
        if assistant:
            flush()
            current = OutputSegment("assistant", assistant.group(1), "assistant")
            continue

        result = re.fullmatch(r"\[(result|error)\]\s*(.*)", raw_line)
        if result:
            flush()
            current = OutputSegment(result.group(1), result.group(2), result.group(1))
            continue

        tagged = re.fullmatch(r"\[([^\]]+)\]\s*(.*)", raw_line)
        if tagged:
            marker = tagged.group(1).strip()
            marker_lower = marker.lower()
            if marker_lower == "notify":
                flush()
                segments.append(OutputSegment("status", tagged.group(2), marker))
                current = None
                continue
            if marker_lower not in _RESERVED_MARKERS:
                flush()
                current = OutputSegment("tool_output", tagged.group(2), marker)
                continue
            flush()
            current = OutputSegment("info", raw_line, marker)
            continue

        if current is None:
            current = OutputSegment("assistant", raw_line)
        elif current.kind in _CONTINUABLE_SEGMENT_KINDS:
            if current.text:
                current.text += "\n"
            current.text += raw_line
        else:
            flush()
            current = OutputSegment("assistant", raw_line)

    flush()
    return segments
