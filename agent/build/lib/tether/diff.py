"""Helpers for parsing unified diffs into UI-friendly structures."""

from __future__ import annotations

import re
def parse_git_diff(raw: str) -> list[dict[str, object]]:
    """Parse a unified diff into per-file patches with hunk counts.

    Args:
        raw: Unified diff text.
    """
    files: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            # Start a new file section when a diff header appears.
            if current:
                files.append(current)
            match = re.match(r"diff --git a/(.+?) b/(.+)", line)
            path = match.group(2) if match else "unknown"
            current = {"path": path, "hunks": 0, "patch_lines": [line]}
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            # Count hunk headers for summary metadata.
            current["hunks"] = int(current["hunks"]) + 1
        current["patch_lines"].append(line)

    if current:
        files.append(current)

    results: list[dict[str, object]] = []
    for entry in files:
        patch_lines = entry.get("patch_lines", [])
        if isinstance(patch_lines, list):
            patch = "\n".join(patch_lines)
        else:
            patch = ""
        results.append(
            {
                "path": entry.get("path", "unknown"),
                "hunks": entry.get("hunks", 0),
                "patch": patch,
            }
        )
    return results
