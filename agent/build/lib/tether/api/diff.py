"""Helpers for running Git diffs inside API endpoints."""

from __future__ import annotations

import shutil
from pathlib import Path
from subprocess import CalledProcessError, run

from tether.api.errors import raise_http_error


def _require_git_binary() -> str:
    git = shutil.which("git")
    if not git:
        raise_http_error("GIT_UNAVAILABLE", "Git executable missing on server", 500)
    return git


def build_git_diff(path: str) -> str:
    """Run `git diff` inside *path* and return the unified patch."""
    repo = Path(path)
    if not repo.is_dir():
        raise_http_error("GIT_ERROR", "Directory not found", 404)
    git = _require_git_binary()

    def run_diff(args: list[str]) -> str:
        result = run([git, "-C", str(repo), *args], capture_output=True, text=True, check=True)
        return result.stdout

    try:
        return run_diff(["diff", "HEAD", "--unified=3", "--no-color"])
    except CalledProcessError as exc:
        stderr = (exc.stderr or "").lower()
        fallback_needed = "unknown revision or path" in stderr or "ambiguous argument" in stderr
        if fallback_needed:
            try:
                return run_diff(["diff", "--unified=3", "--no-color"])
            except CalledProcessError as fallback_exc:
                message = (fallback_exc.stderr or str(fallback_exc)).strip()
                raise_http_error("GIT_ERROR", f"git diff failed: {message}", 500)
        message = (exc.stderr or str(exc)).strip()
        raise_http_error("GIT_ERROR", f"git diff failed: {message}", 500)
