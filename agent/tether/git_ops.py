"""Rich git operations for Tether session workspaces.

Provides read (`git_status`, `git_log`) and write (`git_commit`, `git_push`,
`git_create_branch`, `git_checkout`) operations backed by subprocess git calls.
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


class GitPushResult(BaseModel):
    """Result of a git push operation."""

    success: bool
    remote: str
    branch: str
    message: str | None = None


class PrResult(BaseModel):
    """Result of creating a pull request or merge request."""

    url: str
    number: int
    forge: str  # "github" or "gitlab"
    draft: bool


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


def git_commit(path: str, message: str, add_all: bool = True) -> GitCommit:
    """Stage all changes and create a commit in the repository at *path*.

    Args:
        path: Absolute path to a git repository root.
        message: Commit message (must be non-empty).
        add_all: When True, runs ``git add -A`` before committing.

    Returns:
        A `GitCommit` representing the newly created commit.

    Raises:
        ValueError: Nothing to commit, commit fails, or git is unavailable.
    """
    _require_git(path)

    if add_all:
        _run(["git", "add", "-A"], cwd=path)

    # Check that there is actually something staged
    staged = _run_silent(
        ["git", "diff", "--cached", "--name-only"], cwd=path
    )
    if not staged.strip():
        raise ValueError("Nothing to commit: working tree is clean")

    _run(["git", "commit", "-m", message], cwd=path)

    commit = _last_commit(path)
    if not commit:
        raise ValueError("Commit succeeded but could not retrieve commit info")
    return commit


def git_push(
    path: str,
    remote: str = "origin",
    branch: str | None = None,
    set_upstream: bool = True,
) -> GitPushResult:
    """Push commits to a remote.

    Args:
        path: Absolute path to a git repository root.
        remote: Remote name (default ``"origin"``).
        branch: Branch to push.  When None the current branch is used.
        set_upstream: When True and no upstream is configured, adds
            ``--set-upstream`` so future pushes work without arguments.

    Returns:
        A `GitPushResult` describing the outcome.

    Raises:
        ValueError: Push fails (auth error, remote rejection, etc.).
    """
    _require_git(path)

    current_branch = _current_branch(path) or "HEAD"
    target_branch = branch or current_branch

    cmd = ["git", "push"]
    if set_upstream:
        # Only add --set-upstream when there is no tracking branch yet
        if not _remote_tracking_branch(path):
            cmd.append("--set-upstream")
    cmd += [remote, target_branch]

    try:
        _run(cmd, cwd=path, timeout=60)
    except ValueError as exc:
        raise ValueError(f"git push failed: {exc}") from exc

    remote_url = _remote_url(path, remote)
    return GitPushResult(
        success=True,
        remote=remote_url or remote,
        branch=target_branch,
    )


def git_create_branch(path: str, name: str, checkout: bool = True) -> str:
    """Create a new branch in the repository at *path*.

    Args:
        path: Absolute path to a git repository root.
        name: New branch name.  Must not already exist and must be a valid
            git ref name (no spaces; slashes allowed for namespaced branches).
        checkout: When True (the default) switches to the new branch
            immediately using ``git checkout -b``.

    Returns:
        The name of the newly created branch.

    Raises:
        ValueError: Branch already exists, invalid name, or git fails.
    """
    _require_git(path)
    _validate_branch_name(name)

    if checkout:
        try:
            _run(["git", "checkout", "-b", name], cwd=path)
        except ValueError as exc:
            raise ValueError(f"Could not create branch '{name}': {exc}") from exc
    else:
        try:
            _run(["git", "branch", name], cwd=path)
        except ValueError as exc:
            raise ValueError(f"Could not create branch '{name}': {exc}") from exc

    return name


def detect_forge(remote_url: str) -> str | None:
    """Detect the forge (hosting service) from a git remote URL.

    Args:
        remote_url: The remote URL string (https or ssh).

    Returns:
        ``"github"`` for GitHub URLs, ``"gitlab"`` for GitLab URLs, or
        ``None`` when the forge cannot be determined.
    """
    if not remote_url:
        return None
    url_lower = remote_url.lower()
    if "github.com" in url_lower:
        return "github"
    if "gitlab.com" in url_lower or "gitlab." in url_lower:
        return "gitlab"
    return None


def create_pr(
    path: str,
    title: str,
    body: str = "",
    base: str | None = None,
    draft: bool = False,
    auto_push: bool = True,
) -> PrResult:
    """Create a pull request (GitHub) or merge request (GitLab) from the
    current branch.

    The forge is auto-detected from the ``origin`` remote URL.  Delegates to
    ``gh pr create`` (GitHub) or ``glab mr create`` (GitLab).

    Args:
        path: Absolute path to a git repository root.
        title: PR/MR title (required, non-empty).
        body: PR/MR description (default empty string).
        base: Target branch for the PR/MR.  When None the remote default
            branch is used.
        draft: Create as a draft PR/MR.
        auto_push: When True, push the current branch before creating the
            PR/MR so the remote has the latest commits.

    Returns:
        A `PrResult` with URL, number, forge, and draft flag.

    Raises:
        ValueError: ``gh`` or ``glab`` is not installed, the forge cannot be
            detected, the push fails, or PR/MR creation fails.
    """
    _require_git(path)

    remote_url = _remote_url(path, "origin")
    if not remote_url:
        raise ValueError("No 'origin' remote configured; cannot detect forge")

    forge = detect_forge(remote_url)
    if not forge:
        raise ValueError(
            f"Unsupported forge for remote URL '{remote_url}'. "
            "Only GitHub and GitLab are supported."
        )

    if auto_push:
        try:
            git_push(path, remote="origin", branch=None)
        except ValueError as exc:
            raise ValueError(f"Auto-push before PR creation failed: {exc}") from exc

    if forge == "github":
        return _create_github_pr(path, title, body, base, draft)
    else:
        return _create_gitlab_mr(path, title, body, base, draft)


def git_checkout(path: str, branch: str) -> str:
    """Check out an existing branch in the repository at *path*.

    Args:
        path: Absolute path to a git repository root.
        branch: Branch name to check out.

    Returns:
        The name of the checked-out branch.

    Raises:
        ValueError: Branch does not exist or checkout fails.
    """
    _require_git(path)

    try:
        _run(["git", "checkout", branch], cwd=path)
    except ValueError as exc:
        raise ValueError(f"Could not checkout '{branch}': {exc}") from exc

    return branch


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_INVALID_BRANCH_CHARS_RE = re.compile(r"[ \t\n\x00]")


def _validate_branch_name(name: str) -> None:
    """Raise ValueError if *name* is not a safe git branch name."""
    if not name:
        raise ValueError("Branch name must not be empty")
    if _INVALID_BRANCH_CHARS_RE.search(name):
        raise ValueError(
            f"Branch name '{name}' contains invalid characters (spaces/tabs/newlines)"
        )
    # Delegate the full rule-set to git itself; the check above just catches
    # the most obvious shell-injection risks before we even call subprocess.

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


def _run_tool(args: list[str], cwd: str, tool: str, timeout: int = 60) -> str:
    """Run an external tool (gh/glab) and return stdout, raising on failure.

    Raises:
        ValueError: Tool not found in PATH or command exited non-zero.
    """
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise ValueError(
            f"'{tool}' is not installed or not in PATH. "
            f"Install it to create {'pull requests' if tool == 'gh' else 'merge requests'}."
        )
    except subprocess.TimeoutExpired:
        raise ValueError(f"'{tool}' command timed out after {timeout}s")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise ValueError(f"'{tool}' command failed: {stderr or result.stdout.strip()}")

    return result.stdout.strip()


def _create_github_pr(
    path: str,
    title: str,
    body: str,
    base: str | None,
    draft: bool,
) -> PrResult:
    """Create a GitHub pull request via ``gh pr create``.

    Returns a `PrResult` with the PR URL and number.
    """
    cmd = ["gh", "pr", "create", "--title", title, "--body", body]
    if base:
        cmd += ["--base", base]
    if draft:
        cmd.append("--draft")

    url = _run_tool(cmd, cwd=path, tool="gh")

    # Retrieve the PR number from the URL: .../pull/123
    number = _extract_pr_number(url)
    return PrResult(url=url, number=number, forge="github", draft=draft)


def _create_gitlab_mr(
    path: str,
    title: str,
    body: str,
    base: str | None,
    draft: bool,
) -> PrResult:
    """Create a GitLab merge request via ``glab mr create``.

    Returns a `PrResult` with the MR URL and number.
    """
    cmd = ["glab", "mr", "create", "--title", title, "--description", body, "--yes"]
    if base:
        cmd += ["--target-branch", base]
    if draft:
        cmd.append("--draft")

    output = _run_tool(cmd, cwd=path, tool="glab")

    # glab prints something like:
    # "https://gitlab.com/owner/repo/-/merge_requests/42"
    url = _extract_url_from_output(output)
    if not url:
        raise ValueError(f"glab mr create did not return a URL. Output: {output}")

    number = _extract_pr_number(url)
    return PrResult(url=url, number=number, forge="gitlab", draft=draft)


_URL_RE = re.compile(r"https?://\S+")
_PR_NUMBER_RE = re.compile(r"/(?:pull|merge_requests)/(\d+)")


def _extract_url_from_output(output: str) -> str | None:
    """Extract the first URL from command output."""
    match = _URL_RE.search(output)
    return match.group(0) if match else None


def _extract_pr_number(url: str) -> int:
    """Extract the PR/MR number from a URL like .../pull/42 or .../merge_requests/7."""
    match = _PR_NUMBER_RE.search(url)
    if match:
        return int(match.group(1))
    return 0


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
