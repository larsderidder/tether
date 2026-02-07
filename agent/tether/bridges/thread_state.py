"""Lightweight persisted state for platform thread/topic naming.

Slack and Discord don't require unique thread names, but users generally
prefer them to be unique and consistent with Telegram. We persist a simple
session_id -> thread_name mapping so we can allocate "Name", "Name 2", ...
across restarts.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_mapping(*, path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text("utf-8"))
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            ks = str(k).strip()
            vs = str(v).strip()
            if ks and vs:
                out[ks] = vs
        return out
    except Exception:
        return {}


def save_mapping(*, path: Path, mapping: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(sorted(mapping.items())), indent=2, sort_keys=True) + "\n",
        "utf-8",
    )

