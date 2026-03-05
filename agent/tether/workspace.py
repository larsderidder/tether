"""Workspace manager for Tether server-mode sessions.

Handles git clone, cleanup, and workspace lifecycle for sessions that run
against cloned repositories rather than local directories.

Worktree support
----------------
When a repo URL is encountered for the first time, a full clone is placed in
{data_dir}/repos/{url_hash}/ and registered in the repo registry.  Subsequent
sessions for the same URL create a lightweight git worktree from that shared
clone instead of cloning again.

Detection at cleanup time relies on the ``.git`` file vs ``.git`` directory
distinction: worktrees have a ``.git`` *file*; standalone clones have a
``.git`` *directory*.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Module-level fetch cache
# ---------------------------------------------------------------------------

# Maps resolved repo path -> epoch seconds of last successful fetch attempt.
# Protected by _fetch_cache_lock.
_fetch_cache: dict[str, float] = {}
_fetch_cache_lock = threading.Lock()


class WorkspaceError(Exception):
    """Raised when a workspace operation fails."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class WorkspaceResult(BaseModel):
    """Result returned by :func:`create_workspace`."""

    path: str
    is_worktree: bool
    repo_hash: str | None = None  # None for legacy standalone clones
    working_branch: str  # Branch created in the worktree (always set for worktrees)
    branch_was_forced: bool = False  # True when the caller did not supply working_branch


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


def managed_repos_dir() -> str:
    """Return the shared-clone root directory, creating it if needed.

    Shared clones (used as worktree bases) live under {data_dir}/repos/.
    """
    from tether.settings import settings

    root = Path(settings.data_dir()) / "repos"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def workspace_path(session_id: str) -> str:
    """Return the workspace path for a session.

    Returns {managed_workspaces_dir}/{session_id}/ without creating it.
    """
    return str(Path(managed_workspaces_dir()) / session_id)


def create_workspace(
    url: str,
    session_id: str,
    branch: str | None = None,
    shallow: bool = False,
    working_branch: str | None = None,
) -> WorkspaceResult:
    """Create a workspace for a session, reusing a shared clone when possible.

    On the first call for a given repository URL a full clone is placed in
    {data_dir}/repos/{url_hash}/ and registered in the repo registry.  All
    calls (including the first) create a git worktree from that shared clone
    into the managed workspaces directory.

    A working branch is **always** created in the worktree.  Git worktrees
    cannot share branches, so a unique branch is required for each session.
    When the caller omits *working_branch*, one is generated automatically
    (``tether/<last-6-chars-of-session-id>``) and ``WorkspaceResult.branch_was_forced``
    is set to ``True`` so the caller can log or surface this to the user.

    Args:
        url: Repository URL (HTTPS or SSH).
        session_id: Session identifier; used to name the workspace directory.
        branch: Branch or ref to base the worktree on (default: remote HEAD).
        shallow: Perform a shallow clone if this is the first clone for the URL.
        working_branch: Name for the new branch created in the worktree.
            When omitted a name is generated from session_id.

    Returns:
        WorkspaceResult with the workspace path, is_worktree=True, the repo
        hash key, the working branch name, and a flag indicating whether the
        branch was auto-generated.

    Raises:
        WorkspaceError: Any git operation fails.
    """
    from tether.repo_registry import RepoRegistry, repo_url_hash
    from tether.settings import settings

    registry = RepoRegistry(settings.data_dir())
    url_hash = repo_url_hash(url)
    dest = workspace_path(session_id)

    # Determine the working branch name.  For worktrees a branch is mandatory:
    # git rejects creating a worktree without a distinct branch.
    branch_was_forced = working_branch is None
    branch_name = working_branch or f"tether/{session_id[-6:]}"

    existing = registry.get(url)
    if existing is None:
        # First clone: full clone into the shared repos dir.
        clone_target = str(Path(managed_repos_dir()) / url_hash)
        clone_repo(url, clone_target, branch=branch, shallow=shallow)
        registry.register(url, clone_target)
        shared_path = clone_target
    else:
        shared_path = existing.path
        # Fetch latest from origin so the worktree gets recent commits.
        _fetch_origin(shared_path)

    # Create the worktree with the (possibly auto-generated) branch.
    _worktree_add(shared_path, dest, branch_name, base_ref=branch)
    _configure_git_identity(dest)
    registry.increment_worktrees(url)

    return WorkspaceResult(
        path=dest,
        is_worktree=True,
        repo_hash=url_hash,
        working_branch=branch_name,
        branch_was_forced=branch_was_forced,
    )


def prune_worktrees(shared_clone_path: str) -> None:
    """Run ``git worktree prune`` on a shared clone to remove stale refs.

    Git worktrees that were deleted without ``git worktree remove`` (for
    example after a server crash) leave behind stale administrative entries
    under ``.git/worktrees/``.  Pruning repairs this.

    Failures are silently ignored so the maintenance loop is not disrupted.
    """
    try:
        subprocess.run(
            ["git", "-C", shared_clone_path, "worktree", "prune"],
            capture_output=True,
            timeout=30,
        )
    except Exception:
        pass


def prune_stale_repos(retention_days: int | None = None) -> int:
    """Remove shared clones that have had zero worktrees past the retention period.

    A shared clone is eligible for removal when:
    - Its ``worktree_count`` in the registry is 0, AND
    - Its ``last_used_at`` timestamp is older than *retention_days* days.

    Also runs ``git worktree prune`` on every remaining shared clone to keep
    their worktree metadata tidy.

    Args:
        retention_days: Override for the retention period.  When ``None``,
            ``settings.repo_retention_days()`` is used.

    Returns:
        Number of shared clones removed.
    """
    from tether.repo_registry import RepoRegistry
    from tether.settings import settings

    if retention_days is None:
        retention_days = settings.repo_retention_days()

    registry = RepoRegistry(settings.data_dir())
    cutoff = time.time() - retention_days * 86400
    removed = 0

    for entry in registry.list_repos():
        clone_path = entry.path
        # Prune stale worktree refs on all remaining clones.
        if Path(clone_path).exists():
            prune_worktrees(clone_path)

        if entry.worktree_count > 0:
            continue

        # Parse last_used_at to determine age.
        try:
            import datetime as _dt

            last_used = _dt.datetime.fromisoformat(entry.last_used_at)
            last_ts = last_used.timestamp()
        except Exception:
            continue

        if last_ts < cutoff:
            registry.remove(entry.url)
            removed += 1

    return removed


def list_repo_usage() -> list[dict]:
    """Return disk usage and metadata for all registered shared clones.

    Each entry is a dict::

        {
            "url": str,
            "hash": str,
            "path": str,
            "size_bytes": int,
            "worktree_count": int,
            "last_used_at": str,
        }

    Sorted by ``size_bytes`` descending.
    """
    from tether.repo_registry import RepoRegistry, repo_url_hash
    from tether.settings import settings

    registry = RepoRegistry(settings.data_dir())
    entries = registry.list_repos()

    result: list[dict] = []
    for entry in entries:
        size = dir_size_bytes(entry.path)
        url_hash = repo_url_hash(entry.url)
        result.append(
            {
                "url": entry.url,
                "hash": url_hash,
                "path": entry.path,
                "size_bytes": size,
                "worktree_count": entry.worktree_count,
                "last_used_at": entry.last_used_at,
            }
        )

    result.sort(key=lambda r: r["size_bytes"], reverse=True)
    return result


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

    For git worktrees (detected by the presence of a ``.git`` *file* rather
    than a ``.git`` *directory*), ``git worktree remove --force`` is used and
    the worktree count in the repo registry is decremented.  For ordinary
    standalone clones the directory is removed with ``shutil.rmtree``.

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

    if _is_worktree(str(target)):
        try:
            main_repo = _worktree_main_repo(str(target))
        except WorkspaceError:
            main_repo = None

        try:
            _run(["git", "worktree", "remove", "--force", str(target)], cwd=main_repo or str(target))
        except WorkspaceError:
            # Fall back to plain rmtree if git worktree remove fails.
            shutil.rmtree(str(target), ignore_errors=True)

        if main_repo:
            from tether.repo_registry import RepoRegistry
            from tether.settings import settings

            registry = RepoRegistry(settings.data_dir())
            registry.decrement_worktrees_by_path(main_repo)
        return

    try:
        shutil.rmtree(str(target))
    except OSError as exc:
        raise WorkspaceError(f"Failed to remove workspace '{target}': {exc}") from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_worktree(path: str) -> bool:
    """Return True if *path* is a git worktree (has a .git *file*, not dir)."""
    git_path = Path(path) / ".git"
    return git_path.is_file()


def _worktree_main_repo(path: str) -> str:
    """Return the root path of the main repository for a worktree.

    A worktree's .git file contains a line like::

        gitdir: /abs/path/to/main/.git/worktrees/<name>

    We walk two levels up from that gitdir to reach the main .git directory,
    then one more level to the repository root.

    Raises:
        WorkspaceError: The .git file cannot be parsed or the derived path
            does not look like a git repository.
    """
    git_file = Path(path) / ".git"
    try:
        content = git_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WorkspaceError(f"Cannot read .git file in '{path}': {exc}") from exc

    if not content.startswith("gitdir: "):
        raise WorkspaceError(
            f"Unexpected .git file format in '{path}': {content!r}"
        )

    gitdir = Path(content[len("gitdir: "):].strip())
    # gitdir points to something like /main/.git/worktrees/<name>
    # .parent -> /main/.git/worktrees
    # .parent.parent -> /main/.git
    # .parent.parent.parent -> /main   (repo root)
    main_repo = gitdir.parent.parent.parent
    if not (main_repo / ".git").exists():
        raise WorkspaceError(
            f"Could not determine main repo from worktree .git file: "
            f"'{main_repo}' does not appear to be a git repository"
        )
    return str(main_repo)


def _fetch_origin(repo_path: str) -> None:
    """Run ``git fetch origin`` in *repo_path*, ignoring failures silently.

    Fetching before a worktree add ensures the new worktree starts from a
    reasonably up-to-date state.  Network failures or missing remotes are not
    fatal; the worktree will simply be based on the last locally known state.

    A per-process cache prevents re-fetching the same clone within
    TETHER_GIT_FETCH_CACHE_SECONDS (default 300 s / 5 min).  Set to 0 to
    disable the cache.
    """
    from tether.settings import settings

    resolved = str(Path(repo_path).resolve())
    cache_ttl = settings.git_fetch_cache_seconds()
    now = time.monotonic()

    if cache_ttl > 0:
        with _fetch_cache_lock:
            last = _fetch_cache.get(resolved, 0.0)
            if now - last < cache_ttl:
                return  # Recent fetch; skip.
            # Mark as fetched optimistically before the network call so that
            # concurrent callers don't all pile in at once.
            _fetch_cache[resolved] = now

    try:
        subprocess.run(
            ["git", "-C", repo_path, "fetch", "origin"],
            capture_output=True,
            timeout=settings.git_fetch_timeout(),
        )
    except Exception:
        pass


def _worktree_add(
    main_repo: str,
    dest: str,
    branch_name: str,
    base_ref: str | None = None,
) -> None:
    """Run ``git worktree add`` to create a new worktree at *dest*.

    A fresh branch *branch_name* is created in the worktree.  If *base_ref*
    is given the worktree starts at that ref; otherwise it uses the current
    HEAD of the shared clone.

    Raises:
        WorkspaceError: The git command fails.
    """
    cmd = ["git", "-C", main_repo, "worktree", "add", dest, "-b", branch_name]
    if base_ref:
        cmd.append(base_ref)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_clone_timeout(),
        )
    except FileNotFoundError:
        raise WorkspaceError(
            "git binary not found; ensure git is installed and on PATH"
        )
    except subprocess.TimeoutExpired:
        raise WorkspaceError(
            f"git worktree add timed out after {_clone_timeout()} seconds"
        )

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise WorkspaceError(
            f"git worktree add failed (exit {result.returncode}): {stderr}"
        )


def _run(cmd: list[str], cwd: str | None = None) -> None:
    """Run a subprocess command, raising WorkspaceError on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=_clone_timeout(),
        )
    except FileNotFoundError:
        raise WorkspaceError(
            f"Command not found: {cmd[0]}"
        )
    except subprocess.TimeoutExpired:
        raise WorkspaceError(
            f"Command timed out: {' '.join(cmd)}"
        )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise WorkspaceError(
            f"Command failed (exit {result.returncode}): {' '.join(cmd)}: {stderr}"
        )


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
