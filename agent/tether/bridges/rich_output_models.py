"""Shared models for bridge rich-output rendering."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RenderedBridgeMessage:
    """Rendered chat message with optional full expansion content."""

    text: str
    expansion_text: str | None = None
    expansion_filename: str = "tool-output.txt"


@dataclass(slots=True)
class OutputSegment:
    """Typed chunk of bridge output."""

    kind: str
    text: str
    label: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Serialize the segment for store event metadata."""

        data = {"kind": self.kind, "text": self.text}
        if self.label:
            data["label"] = self.label
        return data
