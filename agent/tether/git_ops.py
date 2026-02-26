"""Rich git operations for Tether session workspaces.

Provides `git_status` and `git_log` which run git subprocesses and parse
their output into structured data models.  These functions are intentionally
read-only; write operations live in separate modules.
"""

from __future__ import annotations

import re
import subprocess
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class GitFileChange(BaseModel):
    """A single changed file reported by git status."""

    path: str
    status: str  # "modified", "added", "deleted", "renamed", "untracked"
    staged: bool


class GitCommit(BaseModel):
    """A single commit from git log."""

    hash: str
    message: str
    author: str
    timestamp: str


class GitStatus(BaseModel):
    """Aggregated git repository state for a workspace."""

    branch: str | None
    remote_url: str | None
    remote_branch: str | None
    ahead: int
    behind: int
    dirty: bool
    changed_files: list[GitFileChange]
    staged_count: int
    unstaged_count: int
    untracked_count: int
    last_commit: GitCommit | None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def git_status(path: str) -> GitStatus:
    """Return the current git status for the repository at *path*.

    Args:
        path: Absolute path to a git repository root.

    Returns:
        A fully populated `GitStatus` instance.

    Raises:
        ValueError: *path* is not a git repository or git is unavailable.
    """
    _require_git(path)

    branch = _current_branch(path)
    remote_url = _remote_url(path, "origin")
    remote_branch = _remote_tracking_branch(path)
    ahead, behind = _ahead_behind(path, remote_branch)
    changed_files = _changed_files(path)
    last_commit = _last_commit(path)

    staged = [f for f in changed_files if f.staged]
    unstaged = [f for f in changed_files if not f.staged and f.status != "untracked"]
    untracked = [f for f in changed_files if f.status == "untracked"]

    return GitStatus(
        branch=branch,
        remote_url=remote_url,
        remote_branch=remote_branch,
        ahead=ahead,
        behind=behind,
        dirty=len(changed_files) > 0,
        changed_files=changed_files,
        staged_count=len(staged),
        unstaged_count=len(unstaged),
        untracked_count=len(untracked),
        last_commit=last_commit,
    )


def git_log(path: str, count: int = 10) -> list[GitCommit]:
    """Return the most recent commits from the repository at *path*.

    Args:
        path: Absolute path to a git repository root.
        count: Maximum number of commits to return (default 10).

    Returns:
        List of `GitCommit` instances, newest first.

    Raises:
        ValueError: *path* is not a git repository or git is unavailable.
    """
    _require_git(path)
    return _recent_commits(path, count)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_GIT_STATUS_MAP: dict[str, str] = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "added",   # copied
    "U": "modified",  # unmerged
    "?": "untracked",
}

_RECORD_SEP = "\x1f"  # unit separator, safe delimiter for git log


def _run(args: list[str], cwd: str, timeout: int = 15) -> str:
    """Run a git command and return stdout, or raise ValueError on failure."""
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise ValueError("git binary not found")
    except subprocess.TimeoutExpired:
        raise ValueError(f"git command timed out: {' '.join(args)}")

    if result.returncode != 0:
        raise ValueError(
            f"git command failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _run_silent(args: list[str], cwd: str, timeout: int = 15, strip: bool = True) -> str:
    """Like _run but returns empty string on non-zero exit instead of raising."""
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip() if strip else result.stdout
    except Exception:
        return ""


def _require_git(path: str) -> None:
    """Raise ValueError if *path* is not a git repository."""
    try:
        _run(["git", "rev-parse", "--git-dir"], cwd=path)
    except ValueError as exc:
        raise ValueError(f"Not a git repository: {path} ({exc})") from exc


def _current_branch(path: str) -> str | None:
    """Return the current branch name, or None for a detached HEAD."""
    out = _run_silent(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    if out == "HEAD":
        # detached HEAD — try to get the short hash instead
        return _run_silent(["git", "rev-parse", "--short", "HEAD"], cwd=path) or None
    return out or None


def _remote_url(path: str, remote: str = "origin") -> str | None:
    """Return the URL for *remote*, or None if not configured."""
    out = _run_silent(["git", "remote", "get-url", remote], cwd=path)
    return out or None


def _remote_tracking_branch(path: str) -> str | None:
    """Return the full remote tracking branch for HEAD (e.g. origin/main)."""
    out = _run_silent(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=path,
    )
    return out or None


def _ahead_behind(path: str, remote_branch: str | None) -> tuple[int, int]:
    """Return (ahead, behind) commit counts vs the remote tracking branch."""
    if not remote_branch:
        return 0, 0
    out = _run_silent(
        ["git", "rev-list", "--left-right", "--count", f"HEAD...{remote_branch}"],
        cwd=path,
    )
    if not out:
        return 0, 0
    parts = out.split()
    if len(parts) == 2:
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            pass
    return 0, 0


def _changed_files(path: str) -> list[GitFileChange]:
    """Return all changed files (staged, unstaged, untracked)."""
    # --porcelain=v1 format: XY <path>
    # X = staged status, Y = unstaged status
    # Do NOT strip() here: a leading space is a valid status character
    out = _run_silent(["git", "status", "--porcelain=v1", "-z"], cwd=path, strip=False)
    if not out:
        return []

    files: list[GitFileChange] = []
    entries = out.split("\x00")
    i = 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) < 4:
            i += 1
            continue

        xy = entry[:2]
        file_path = entry[3:]
        x, y = xy[0], xy[1]

        # Handle rename: next entry is original path, skip it
        if x == "R" or y == "R":
            i += 1  # skip original path entry

        if x != " " and x != "?":
            status = _GIT_STATUS_MAP.get(x, "modified")
            files.append(GitFileChange(path=file_path, status=status, staged=True))

        if y == "?":
            files.append(GitFileChange(path=file_path, status="untracked", staged=False))
        elif y != " ":
            status = _GIT_STATUS_MAP.get(y, "modified")
            files.append(GitFileChange(path=file_path, status=status, staged=False))

        i += 1

    return files


def _last_commit(path: str) -> GitCommit | None:
    """Return the most recent commit, or None if the repo has no commits."""
    commits = _recent_commits(path, count=1)
    return commits[0] if commits else None


def _recent_commits(path: str, count: int) -> list[GitCommit]:
    """Return up to *count* recent commits from HEAD."""
    sep = _RECORD_SEP
    fmt = f"%h{sep}%s{sep}%an{sep}%aI"
    out = _run_silent(
        ["git", "log", f"-{count}", f"--pretty=format:{fmt}"],
        cwd=path,
    )
    if not out:
        return []

    commits: list[GitCommit] = []
    for line in out.splitlines():
        parts = line.split(sep, 3)
        if len(parts) == 4:
            commits.append(
                GitCommit(
                    hash=parts[0],
                    message=parts[1],
                    author=parts[2],
                    timestamp=parts[3],
                )
            )
    return commits
