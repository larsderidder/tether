"""Utility helpers for normalizing directories and detecting Git repositories."""

from __future__ import annotations

from pathlib import Path


def normalize_directory_path(path: str) -> str:
    """Return a normalized absolute path for the provided directory string."""
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=False)
    except FileNotFoundError:
        resolved = candidate
    return str(resolved)


def has_git_repository(path: str) -> bool:
    """Return True if the directory contains a Git repository.

    Accepts both standalone clones (which have a ``.git`` *directory*) and
    git worktrees (which have a ``.git`` *file* pointing at the main repo).
    """
    try:
        repo = Path(path)
        git_path = repo / ".git"
        return git_path.exists()  # True for both .git/ directory and .git file
    except Exception:
        return False
