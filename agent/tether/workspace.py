"""Workspace manager for Tether server-mode sessions.

Handles git clone, cleanup, and workspace lifecycle for sessions that run
against cloned repositories rather than local directories.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class WorkspaceError(Exception):
    """Raised when a workspace operation fails."""


# ---------------------------------------------------------------------------
# Disk usage
# ---------------------------------------------------------------------------


def dir_size_bytes(path: str) -> int:
    """Return the total size in bytes of all files under *path*.

    Follows symlinks for files but does not follow them for directories
    (matches ``du -L --bytes`` behaviour). Returns 0 if the path does not
    exist or cannot be stat'd.
    """
    total = 0
    try:
        for entry in Path(path).rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def list_workspace_usage() -> list[dict]:
    """Return disk usage for every managed workspace directory.

    Each entry is a dict::

        {
            "session_id": str,
            "path": str,
            "size_bytes": int,
        }

    Only directories that exist under the managed workspaces root are
    returned. The list is sorted by ``size_bytes`` descending.
    """
    root = Path(managed_workspaces_dir())
    if not root.exists():
        return []

    entries: list[dict] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return []

    for child in children:
        if not child.is_dir():
            continue
        session_id = child.name
        size = dir_size_bytes(str(child))
        entries.append({"session_id": session_id, "path": str(child), "size_bytes": size})

    entries.sort(key=lambda e: e["size_bytes"], reverse=True)
    return entries


def find_orphan_workspaces(known_session_ids: set[str]) -> list[dict]:
    """Return workspace directories that have no matching session.

    Useful for identifying leftover directories after sessions are deleted
    outside the normal ``delete_session`` flow (for example, a server crash
    mid-delete or a manual database wipe).

    Args:
        known_session_ids: Set of session IDs currently in the store.

    Returns:
        List of ``{session_id, path, size_bytes}`` dicts for orphaned dirs.
    """
    all_workspaces = list_workspace_usage()
    return [w for w in all_workspaces if w["session_id"] not in known_session_ids]


def cleanup_orphan_workspace(path: str) -> None:
    """Remove an orphaned workspace directory.

    Delegates to :func:`cleanup_workspace`, which enforces that the path
    is under the managed workspaces root.
    """
    cleanup_workspace(path)


def managed_workspaces_dir() -> str:
    """Return the managed workspaces root directory, creating it if needed.

    Uses TETHER_WORKSPACE_DIR if set; otherwise falls back to
    {data_dir}/workspaces/.
    """
    override = os.environ.get("TETHER_WORKSPACE_DIR", "").strip()
    if override:
        root = Path(override)
    else:
        from tether.settings import settings

        root = Path(settings.data_dir()) / "workspaces"

    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def workspace_path(session_id: str) -> str:
    """Return the workspace path for a session.

    Returns {managed_workspaces_dir}/{session_id}/ without creating it.
    """
    return str(Path(managed_workspaces_dir()) / session_id)


def clone_repo(
    url: str,
    target_dir: str,
    branch: str | None = None,
    shallow: bool = False,
) -> str:
    """Clone a git repository into target_dir.

    Supports SSH and HTTPS URLs.  The target_dir must not already exist;
    git clone will create it.

    Args:
        url: Repository URL (HTTPS or SSH).
        target_dir: Destination path for the clone.
        branch: Optional branch, tag, or ref to check out.
        shallow: When True, performs a shallow clone (--depth 1).

    Returns:
        The absolute path to the cloned repository (same as target_dir,
        resolved to an absolute path).

    Raises:
        WorkspaceError: Clone failed, URL unreachable, or authentication
            error.  The error message includes captured stderr output.
    """
    timeout = _clone_timeout()

    cmd: list[str] = ["git", "clone"]

    if branch:
        cmd += ["--branch", branch]

    if shallow:
        cmd += ["--depth", "1"]

    cmd += [url, target_dir]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise WorkspaceError(
            "git binary not found; ensure git is installed and on PATH"
        )
    except subprocess.TimeoutExpired:
        raise WorkspaceError(
            f"git clone timed out after {timeout} seconds (URL: {url})"
        )

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise WorkspaceError(
            f"git clone failed (exit {result.returncode}): {stderr}"
        )

    resolved = str(Path(target_dir).resolve())
    _configure_git_identity(resolved)
    return resolved


def cleanup_workspace(path: str) -> None:
    """Remove a workspace directory.

    A safety check ensures the path is located under the managed workspaces
    root before deletion, preventing accidental removal of arbitrary
    directories.

    Args:
        path: Absolute path to the workspace to remove.

    Raises:
        WorkspaceError: The path is outside the managed workspaces root, or
            removal fails.
    """
    root = Path(managed_workspaces_dir()).resolve()
    target = Path(path).resolve()

    try:
        target.relative_to(root)
    except ValueError:
        raise WorkspaceError(
            f"Refusing to remove '{target}': path is outside managed workspaces "
            f"root '{root}'"
        )

    if not target.exists():
        return

    try:
        shutil.rmtree(str(target))
    except OSError as exc:
        raise WorkspaceError(f"Failed to remove workspace '{target}': {exc}") from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clone_timeout() -> int:
    """Return the configured clone timeout in seconds (default 120)."""
    raw = os.environ.get("TETHER_GIT_CLONE_TIMEOUT", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return 120


def _git_user_name() -> str:
    """Return the git user.name to apply to cloned workspaces."""
    raw = os.environ.get("TETHER_GIT_USER_NAME", "").strip()
    return raw if raw else "Tether"


def _git_user_email() -> str:
    """Return the git user.email to apply to cloned workspaces."""
    raw = os.environ.get("TETHER_GIT_USER_EMAIL", "").strip()
    return raw if raw else "tether@localhost"


def _configure_git_identity(repo_path: str) -> None:
    """Set local git user.name and user.email in a cloned workspace.

    Uses local config so the server's global git config is unaffected and
    different sessions can carry different identities in the future.

    Failures are silently ignored: a missing git binary or a read-only repo
    are unlikely during normal operation and should not block session creation.
    """
    name = _git_user_name()
    email = _git_user_email()

    for key, value in (("user.name", name), ("user.email", email)):
        try:
            subprocess.run(
                ["git", "-C", repo_path, "config", key, value],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass
