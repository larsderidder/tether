"""Unit tests for the workspace manager module."""

from __future__ import annotations

import os
import subprocess

import pytest

from tether.workspace import (
    WorkspaceError,
    WorkspaceResult,
    cleanup_workspace,
    clone_repo,
    create_workspace,
    managed_workspaces_dir,
    managed_repos_dir,
    workspace_path,
    _is_worktree,
    _worktree_main_repo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bare_repo(path: str) -> None:
    """Initialise a bare git repository with one commit at *path*."""
    subprocess.run(["git", "init", "--bare", path], check=True, capture_output=True)


def _make_source_repo(path: str) -> None:
    """Initialise a regular git repository with at least one commit at *path*.

    Creates a 'main' branch and an additional 'feature' branch so that
    branch-clone tests have something to check out.
    """
    subprocess.run(["git", "init", "-b", "main", path], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", path, "config", "user.email", "test@test.test"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", path, "config", "user.name", "Test"],
        check=True, capture_output=True,
    )

    readme = os.path.join(path, "README.md")
    with open(readme, "w") as f:
        f.write("# test repo\n")

    subprocess.run(["git", "-C", path, "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", path, "commit", "-m", "Initial commit"],
        check=True, capture_output=True,
    )

    # Create a feature branch
    subprocess.run(
        ["git", "-C", path, "checkout", "-b", "feature"],
        check=True, capture_output=True,
    )
    feature_file = os.path.join(path, "feature.txt")
    with open(feature_file, "w") as f:
        f.write("feature\n")
    subprocess.run(["git", "-C", path, "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", path, "commit", "-m", "Feature commit"],
        check=True, capture_output=True,
    )

    # Return to main
    subprocess.run(
        ["git", "-C", path, "checkout", "main"],
        check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# managed_workspaces_dir / workspace_path
# ---------------------------------------------------------------------------


class TestManagedWorkspacesDir:
    def test_uses_env_override(self, tmp_path, monkeypatch):
        """TETHER_WORKSPACE_DIR env var overrides the default location."""
        custom = str(tmp_path / "custom_ws")
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", custom)

        result = managed_workspaces_dir()

        assert result == custom
        assert os.path.isdir(result)

    def test_creates_directory_if_missing(self, tmp_path, monkeypatch):
        """The workspaces directory is created when it does not yet exist."""
        custom = str(tmp_path / "new_ws_dir")
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", custom)

        assert not os.path.exists(custom)
        managed_workspaces_dir()
        assert os.path.isdir(custom)

    def test_defaults_to_data_dir_workspaces(self, tmp_path, monkeypatch):
        """Without TETHER_WORKSPACE_DIR, defaults to {data_dir}/workspaces."""
        monkeypatch.delenv("TETHER_WORKSPACE_DIR", raising=False)
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

        result = managed_workspaces_dir()

        assert result.endswith("workspaces")
        assert os.path.isdir(result)


class TestWorkspacePath:
    def test_returns_path_under_managed_root(self, tmp_path, monkeypatch):
        """workspace_path returns a path under the managed root."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))

        result = workspace_path("sess_abc123")

        assert result.endswith("sess_abc123")
        assert managed_workspaces_dir() in result


# ---------------------------------------------------------------------------
# clone_repo
# ---------------------------------------------------------------------------


class TestCloneRepo:
    def test_clone_local_repo(self, tmp_path, monkeypatch):
        """Clone a local repository (no network required)."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))

        src = str(tmp_path / "source")
        _make_source_repo(src)

        dest = workspace_path("sess_clone_basic")
        result = clone_repo(src, dest)

        assert result == dest
        assert os.path.isdir(dest)
        assert os.path.isfile(os.path.join(dest, "README.md"))

    def test_clone_with_branch(self, tmp_path, monkeypatch):
        """Cloning with an explicit branch checks out that branch."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))

        src = str(tmp_path / "source")
        _make_source_repo(src)

        dest = workspace_path("sess_clone_branch")
        clone_repo(src, dest, branch="feature")

        assert os.path.isfile(os.path.join(dest, "feature.txt"))

    def test_shallow_clone(self, tmp_path, monkeypatch):
        """Shallow clone (--depth 1) succeeds and produces a working tree."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))

        src = str(tmp_path / "source")
        _make_source_repo(src)

        dest = workspace_path("sess_clone_shallow")
        result = clone_repo(src, dest, shallow=True)

        assert os.path.isdir(result)
        assert os.path.isfile(os.path.join(result, "README.md"))

        # Confirm depth: only one commit should be visible
        log = subprocess.run(
            ["git", "-C", result, "log", "--oneline"],
            capture_output=True, text=True,
        )
        assert len(log.stdout.strip().splitlines()) == 1

    def test_returns_absolute_path(self, tmp_path, monkeypatch):
        """clone_repo always returns an absolute path."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))

        src = str(tmp_path / "source")
        _make_source_repo(src)

        dest = workspace_path("sess_abs")
        result = clone_repo(src, dest)

        assert os.path.isabs(result)

    def test_bad_url_raises_workspace_error(self, tmp_path, monkeypatch):
        """Cloning a non-existent URL raises WorkspaceError."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))

        dest = workspace_path("sess_bad_url")

        with pytest.raises(WorkspaceError, match="git clone failed"):
            clone_repo("/nonexistent/url/that/does/not/exist", dest)

    def test_missing_git_binary_raises_workspace_error(self, tmp_path, monkeypatch):
        """WorkspaceError is raised when git is not found on PATH."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("PATH", "")

        dest = workspace_path("sess_no_git")

        with pytest.raises(WorkspaceError, match="git binary not found"):
            clone_repo("https://example.com/repo.git", dest)

    def test_timeout_raises_workspace_error(self, tmp_path, monkeypatch):
        """WorkspaceError is raised when the clone exceeds the timeout."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_GIT_CLONE_TIMEOUT", "0")

        src = str(tmp_path / "source")
        _make_source_repo(src)

        dest = workspace_path("sess_timeout")

        with pytest.raises(WorkspaceError, match="timed out"):
            clone_repo(src, dest)


# ---------------------------------------------------------------------------
# cleanup_workspace
# ---------------------------------------------------------------------------


class TestCleanupWorkspace:
    def test_cleanup_removes_directory(self, tmp_path, monkeypatch):
        """cleanup_workspace removes the target directory."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))

        src = str(tmp_path / "source")
        _make_source_repo(src)

        dest = workspace_path("sess_cleanup")
        clone_repo(src, dest)
        assert os.path.isdir(dest)

        cleanup_workspace(dest)
        assert not os.path.exists(dest)

    def test_cleanup_nonexistent_path_is_noop(self, tmp_path, monkeypatch):
        """Cleaning up an already-missing path does not raise."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))

        dest = workspace_path("sess_missing")
        # Must not raise even though dest does not exist
        cleanup_workspace(dest)

    def test_cleanup_outside_managed_root_raises(self, tmp_path, monkeypatch):
        """cleanup_workspace refuses to delete paths outside the managed root."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))

        outside = str(tmp_path / "important_dir")
        os.makedirs(outside)

        with pytest.raises(WorkspaceError, match="outside managed workspaces root"):
            cleanup_workspace(outside)

    def test_cleanup_traversal_attack_rejected(self, tmp_path, monkeypatch):
        """Path traversal attempts are blocked."""
        ws_root = str(tmp_path / "ws")
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", ws_root)
        managed_workspaces_dir()  # ensure root exists

        traversal = ws_root + "/../important_dir"

        with pytest.raises(WorkspaceError, match="outside managed workspaces root"):
            cleanup_workspace(traversal)


# ---------------------------------------------------------------------------
# Git identity in cloned workspaces
# ---------------------------------------------------------------------------


class TestGitIdentityAfterClone:
    """Verify that user.name and user.email are written into the local git
    config after clone_repo() completes."""

    def _read_git_config(self, repo_path: str, key: str) -> str:
        result = subprocess.run(
            ["git", "-C", repo_path, "config", "--local", key],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def test_default_identity_set_after_clone(self, tmp_path, monkeypatch):
        """Default user.name and user.email are written to local git config."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.delenv("TETHER_GIT_USER_NAME", raising=False)
        monkeypatch.delenv("TETHER_GIT_USER_EMAIL", raising=False)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        dest = workspace_path("sess_identity_default")
        clone_repo(src, dest)

        assert self._read_git_config(dest, "user.name") == "Tether"
        assert self._read_git_config(dest, "user.email") == "tether@localhost"

    def test_custom_identity_set_after_clone(self, tmp_path, monkeypatch):
        """TETHER_GIT_USER_NAME / TETHER_GIT_USER_EMAIL are respected."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_GIT_USER_NAME", "CI Bot")
        monkeypatch.setenv("TETHER_GIT_USER_EMAIL", "ci@example.com")

        src = str(tmp_path / "source")
        _make_source_repo(src)

        dest = workspace_path("sess_identity_custom")
        clone_repo(src, dest)

        assert self._read_git_config(dest, "user.name") == "CI Bot"
        assert self._read_git_config(dest, "user.email") == "ci@example.com"

    def test_identity_is_local_not_global(self, tmp_path, monkeypatch):
        """Identity is stored in the repo's local config, not --global."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))

        src = str(tmp_path / "source")
        _make_source_repo(src)

        dest = workspace_path("sess_identity_scope")
        clone_repo(src, dest)

        # --local must return a value; --global must not override it
        local_result = subprocess.run(
            ["git", "-C", dest, "config", "--local", "user.name"],
            capture_output=True, text=True,
        )
        assert local_result.returncode == 0
        assert local_result.stdout.strip() != ""

    def test_commit_works_with_configured_identity(self, tmp_path, monkeypatch):
        """A commit can be created inside the workspace using the configured identity."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.delenv("TETHER_GIT_USER_NAME", raising=False)
        monkeypatch.delenv("TETHER_GIT_USER_EMAIL", raising=False)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        dest = workspace_path("sess_commit_test")
        clone_repo(src, dest)

        # Create a new file and commit it
        new_file = os.path.join(dest, "new.txt")
        with open(new_file, "w") as f:
            f.write("hello\n")

        subprocess.run(["git", "-C", dest, "add", "."], check=True, capture_output=True)
        result = subprocess.run(
            ["git", "-C", dest, "commit", "-m", "Test commit"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"commit failed: {result.stderr}"


# ---------------------------------------------------------------------------
# Worktree detection helpers
# ---------------------------------------------------------------------------


class TestWorktreeDetection:
    def test_is_worktree_false_for_standalone_clone(self, tmp_path, monkeypatch):
        """A full clone has a .git directory, so is_worktree returns False."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

        src = str(tmp_path / "source")
        _make_source_repo(src)

        dest = workspace_path("sess_detect_clone")
        clone_repo(src, dest)

        assert not _is_worktree(dest)

    def test_is_worktree_true_for_worktree(self, tmp_path, monkeypatch):
        """A git worktree has a .git file, so is_worktree returns True."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

        src = str(tmp_path / "source")
        _make_source_repo(src)

        # Create a standalone clone first, then add a worktree from it.
        shared = str(tmp_path / "shared_clone")
        clone_repo(src, shared)

        wt = str(tmp_path / "ws" / "my_worktree")
        subprocess.run(
            ["git", "-C", shared, "worktree", "add", wt, "-b", "tether/wt1"],
            check=True, capture_output=True,
        )

        assert _is_worktree(wt)

    def test_worktree_main_repo_resolves_correctly(self, tmp_path, monkeypatch):
        """_worktree_main_repo returns the path of the shared clone."""
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

        src = str(tmp_path / "source")
        _make_source_repo(src)

        shared = str(tmp_path / "shared_clone")
        clone_repo(src, shared)

        wt = str(tmp_path / "ws" / "my_worktree2")
        subprocess.run(
            ["git", "-C", shared, "worktree", "add", wt, "-b", "tether/wt2"],
            check=True, capture_output=True,
        )

        main_repo = _worktree_main_repo(wt)
        # Both paths should resolve to the same directory.
        import pathlib
        assert pathlib.Path(main_repo).resolve() == pathlib.Path(shared).resolve()

    def test_worktree_main_repo_raises_for_non_worktree(self, tmp_path):
        """_worktree_main_repo raises WorkspaceError if there is no .git file."""
        non_wt = str(tmp_path / "random_dir")
        os.makedirs(non_wt)
        with pytest.raises(WorkspaceError):
            _worktree_main_repo(non_wt)


# ---------------------------------------------------------------------------
# create_workspace
# ---------------------------------------------------------------------------


class TestCreateWorkspace:
    def _setup(self, tmp_path, monkeypatch):
        ws_dir = str(tmp_path / "ws")
        data_dir = str(tmp_path / "data")
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", ws_dir)
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", data_dir)
        return ws_dir, data_dir

    def test_first_session_creates_shared_clone_and_worktree(self, tmp_path, monkeypatch):
        """First call for a URL creates a shared clone and a worktree."""
        ws_dir, data_dir = self._setup(tmp_path, monkeypatch)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        result = create_workspace(src, "sess_first")

        assert isinstance(result, WorkspaceResult)
        assert result.is_worktree is True
        assert result.repo_hash is not None

        # Workspace directory should exist and be a worktree.
        assert os.path.isdir(result.path)
        assert _is_worktree(result.path)

        # Shared clone should exist under repos/.
        repos_root = os.path.join(data_dir, "repos", result.repo_hash)
        assert os.path.isdir(repos_root)
        assert not _is_worktree(repos_root)

    def test_second_session_reuses_shared_clone(self, tmp_path, monkeypatch):
        """Second call for the same URL adds a worktree without cloning again."""
        self._setup(tmp_path, monkeypatch)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        result1 = create_workspace(src, "sess_reuse_a")
        result2 = create_workspace(src, "sess_reuse_b")

        assert result1.repo_hash == result2.repo_hash
        # Both are worktrees.
        assert _is_worktree(result1.path)
        assert _is_worktree(result2.path)
        # They should point to different directories.
        assert result1.path != result2.path

    def test_second_session_same_shared_clone_path(self, tmp_path, monkeypatch):
        """Both worktrees share the same underlying clone directory."""
        ws_dir, data_dir = self._setup(tmp_path, monkeypatch)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        result1 = create_workspace(src, "sess_shared_a")
        result2 = create_workspace(src, "sess_shared_b")

        main1 = _worktree_main_repo(result1.path)
        main2 = _worktree_main_repo(result2.path)
        import pathlib
        assert pathlib.Path(main1).resolve() == pathlib.Path(main2).resolve()

    def test_workspace_contains_repo_files(self, tmp_path, monkeypatch):
        """The worktree contains the repository files."""
        self._setup(tmp_path, monkeypatch)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        result = create_workspace(src, "sess_files")
        assert os.path.isfile(os.path.join(result.path, "README.md"))

    def test_registry_worktree_count_incremented(self, tmp_path, monkeypatch):
        """The repo registry tracks the number of live worktrees."""
        ws_dir, data_dir = self._setup(tmp_path, monkeypatch)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        result1 = create_workspace(src, "sess_count_a")
        result2 = create_workspace(src, "sess_count_b")

        from tether.repo_registry import RepoRegistry
        registry = RepoRegistry(data_dir)
        entry = registry.get(src)
        assert entry is not None
        assert entry.worktree_count == 2

    def test_working_branch_named_correctly(self, tmp_path, monkeypatch):
        """The worktree branch uses the session ID suffix by default."""
        self._setup(tmp_path, monkeypatch)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        result = create_workspace(src, "sess_branch_test")

        branch_result = subprocess.run(
            ["git", "-C", result.path, "branch", "--show-current"],
            capture_output=True, text=True,
        )
        branch = branch_result.stdout.strip()
        assert branch.startswith("tether/")

    def test_custom_working_branch(self, tmp_path, monkeypatch):
        """The caller can specify an explicit working branch name."""
        self._setup(tmp_path, monkeypatch)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        result = create_workspace(src, "sess_custom_branch", working_branch="my-custom-branch")

        branch_result = subprocess.run(
            ["git", "-C", result.path, "branch", "--show-current"],
            capture_output=True, text=True,
        )
        assert branch_result.stdout.strip() == "my-custom-branch"

    def test_git_identity_configured_in_worktree(self, tmp_path, monkeypatch):
        """Git user.name and user.email are written into the worktree local config."""
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setenv("TETHER_GIT_USER_NAME", "Tether Bot")
        monkeypatch.setenv("TETHER_GIT_USER_EMAIL", "bot@tether.test")

        src = str(tmp_path / "source")
        _make_source_repo(src)

        result = create_workspace(src, "sess_identity_wt")

        name = subprocess.run(
            ["git", "-C", result.path, "config", "--local", "user.name"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert name == "Tether Bot"


# ---------------------------------------------------------------------------
# cleanup_workspace: worktree-aware
# ---------------------------------------------------------------------------


class TestCleanupWorktreeAware:
    def test_cleanup_worktree_removes_directory(self, tmp_path, monkeypatch):
        """cleanup_workspace removes a worktree directory."""
        ws_dir = str(tmp_path / "ws")
        data_dir = str(tmp_path / "data")
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", ws_dir)
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", data_dir)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        result = create_workspace(src, "sess_cleanup_wt")
        assert os.path.isdir(result.path)

        cleanup_workspace(result.path)
        assert not os.path.exists(result.path)

    def test_cleanup_worktree_decrements_count(self, tmp_path, monkeypatch):
        """cleanup_workspace decrements the registry worktree count."""
        ws_dir = str(tmp_path / "ws")
        data_dir = str(tmp_path / "data")
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", ws_dir)
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", data_dir)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        r1 = create_workspace(src, "sess_decr_a")
        r2 = create_workspace(src, "sess_decr_b")

        cleanup_workspace(r1.path)

        from tether.repo_registry import RepoRegistry
        registry = RepoRegistry(data_dir)
        entry = registry.get(src)
        assert entry.worktree_count == 1

        cleanup_workspace(r2.path)
        entry = registry.get(src)
        assert entry.worktree_count == 0

    def test_cleanup_fallback_on_plain_directory(self, tmp_path, monkeypatch):
        """cleanup_workspace still works for non-worktree managed directories."""
        ws_dir = str(tmp_path / "ws")
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", ws_dir)
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

        # Create a plain managed directory (no git worktree).
        plain = os.path.join(ws_dir, "sess_plain")
        os.makedirs(plain)
        open(os.path.join(plain, "f.txt"), "w").close()

        cleanup_workspace(plain)
        assert not os.path.exists(plain)
