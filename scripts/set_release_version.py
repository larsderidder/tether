#!/usr/bin/env python3
"""Set the project version from a GitHub release tag."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

PROJECT_VERSION_RE = re.compile(r'(?m)^version = "[^"]+"$')


def release_tag_to_python_version(release_tag: str) -> str:
    return release_tag.lstrip("v").replace("-rc", "rc")


def set_project_version(pyproject_path: Path, version: str) -> None:
    text = pyproject_path.read_text(encoding="utf-8")
    updated, replacements = PROJECT_VERSION_RE.subn(f'version = "{version}"', text, count=1)
    if replacements != 1:
        raise SystemExit(f"could not update project version in {pyproject_path}")
    pyproject_path.write_text(updated, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--pyproject", type=Path, default=Path("agent/pyproject.toml"))
    args = parser.parse_args()

    version = release_tag_to_python_version(args.release_tag)
    set_project_version(args.pyproject, version)
    print(f"Set {args.pyproject} version to {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
