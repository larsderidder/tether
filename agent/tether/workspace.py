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

    return str(Path(target_dir).resolve())


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
