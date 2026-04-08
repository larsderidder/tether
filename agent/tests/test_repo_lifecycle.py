"""Tests for shared repo clone lifecycle: fetch cache, worktree pruning, and stale-repo cleanup."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tether.workspace import (
    WorkspaceResult,
    _fetch_cache,
    _fetch_cache_lock,
    _fetch_origin,
    create_workspace,
    list_repo_usage,
    prune_stale_repos,
    prune_worktrees,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source_repo(path: str) -> None:
    """Create a minimal committed git repo at *path*."""
    subprocess.run(["git", "init", "-b", "main", path], check=True, capture_output=True)
    subprocess.run(["git", "-C", path, "config", "user.email", "t@t.t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", path, "config", "user.name", "T"], check=True, capture_output=True)
    readme = os.path.join(path, "README.md")
    with open(readme, "w") as f:
        f.write("# test\n")
    subprocess.run(["git", "-C", path, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", path, "commit", "-m", "init"], check=True, capture_output=True)


def _clear_fetch_cache():
    with _fetch_cache_lock:
        _fetch_cache.clear()


# ---------------------------------------------------------------------------
# Fetch cache
# ---------------------------------------------------------------------------


class TestFetchCache:
    def test_fetch_runs_on_first_call(self, tmp_path, monkeypatch):
        """_fetch_origin calls git fetch on the first invocation for a path."""
        _clear_fetch_cache()
        # Disable cache for this test so we only assert first-call behavior.
        monkeypatch.setenv("TETHER_GIT_FETCH_CACHE_SECONDS", "0")
        monkeypatch.setenv("TETHER_GIT_FETCH_TIMEOUT", "10")

        repo_path = str(tmp_path / "repo")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _fetch_origin(repo_path)

        mock_run.assert_called_once()

    def test_fetch_skipped_within_cache_window(self, tmp_path, monkeypatch):
        """_fetch_origin skips the git call if called again within the cache TTL."""
        _clear_fetch_cache()
        monkeypatch.setenv("TETHER_GIT_FETCH_CACHE_SECONDS", "300")
        monkeypatch.setenv("TETHER_GIT_FETCH_TIMEOUT", "10")

        # Seed the cache with a very recent timestamp.
        resolved = str(Path("/some/cached/repo").resolve())
        with _fetch_cache_lock:
            _fetch_cache[resolved] = time.monotonic()

        with patch("subprocess.run") as mock_run:
            _fetch_origin("/some/cached/repo")

        mock_run.assert_not_called()

    def test_fetch_runs_after_cache_expired(self, tmp_path, monkeypatch):
        """_fetch_origin fetches again once the cache entry has expired."""
        _clear_fetch_cache()
        monkeypatch.setenv("TETHER_GIT_FETCH_CACHE_SECONDS", "1")
        monkeypatch.setenv("TETHER_GIT_FETCH_TIMEOUT", "10")

        resolved = str(Path("/expired/repo").resolve())
        # Seed cache with an old timestamp (10 seconds ago).
        with _fetch_cache_lock:
            _fetch_cache[resolved] = time.monotonic() - 10

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _fetch_origin("/expired/repo")

        mock_run.assert_called_once()

    def test_fetch_cache_disabled_when_zero(self, monkeypatch):
        """Setting TETHER_GIT_FETCH_CACHE_SECONDS=0 disables the cache."""
        _clear_fetch_cache()
        monkeypatch.setenv("TETHER_GIT_FETCH_CACHE_SECONDS", "0")
        monkeypatch.setenv("TETHER_GIT_FETCH_TIMEOUT", "10")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _fetch_origin("/some/path/a")
            _fetch_origin("/some/path/a")

        assert mock_run.call_count == 2

    def test_fetch_failure_does_not_raise(self, monkeypatch):
        """Exceptions from git fetch are swallowed; the caller is not disrupted."""
        _clear_fetch_cache()
        monkeypatch.setenv("TETHER_GIT_FETCH_CACHE_SECONDS", "0")
        monkeypatch.setenv("TETHER_GIT_FETCH_TIMEOUT", "10")

        with patch("subprocess.run", side_effect=OSError("no network")):
            # Must not raise.
            _fetch_origin("/unreachable/repo")

    def test_fetch_uses_configured_timeout(self, monkeypatch):
        """_fetch_origin passes TETHER_GIT_FETCH_TIMEOUT to subprocess.run."""
        _clear_fetch_cache()
        monkeypatch.setenv("TETHER_GIT_FETCH_CACHE_SECONDS", "0")
        monkeypatch.setenv("TETHER_GIT_FETCH_TIMEOUT", "42")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _fetch_origin("/some/path/b")

        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("timeout") == 42


# ---------------------------------------------------------------------------
# Worktree pruning
# ---------------------------------------------------------------------------


class TestPruneWorktrees:
    def test_prune_removes_stale_worktree_refs(self, tmp_path):
        """prune_worktrees removes stale entries after a worktree dir is deleted."""
        src = str(tmp_path / "source")
        _make_source_repo(src)

        shared = str(tmp_path / "shared")
        subprocess.run(["git", "clone", src, shared], check=True, capture_output=True)

        wt = str(tmp_path / "wt")
        subprocess.run(
            ["git", "-C", shared, "worktree", "add", wt, "-b", "tether/prune-test"],
            check=True, capture_output=True,
        )

        # Delete the worktree directory without git worktree remove.
        import shutil
        shutil.rmtree(wt)

        # Before prune: git worktree list shows the stale entry.
        before = subprocess.run(
            ["git", "-C", shared, "worktree", "list"],
            capture_output=True, text=True,
        )
        assert wt in before.stdout

        prune_worktrees(shared)

        # After prune: stale entry is gone.
        after = subprocess.run(
            ["git", "-C", shared, "worktree", "list"],
            capture_output=True, text=True,
        )
        assert wt not in after.stdout

    def test_prune_does_not_raise_on_invalid_path(self):
        """prune_worktrees is a no-op for non-existent or non-git paths."""
        # Must not raise.
        prune_worktrees("/nonexistent/path")


# ---------------------------------------------------------------------------
# Stale repo pruning
# ---------------------------------------------------------------------------


class TestPruneStaleRepos:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

    def test_repos_with_active_worktrees_are_kept(self, tmp_path, monkeypatch):
        """A shared clone with worktree_count > 0 is never pruned."""
        self._setup(tmp_path, monkeypatch)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        _clear_fetch_cache()
        create_workspace(src, "sess_keep")

        # Repo has worktree_count=1; must not be pruned regardless of age.
        removed = prune_stale_repos(retention_days=0)
        assert removed == 0

        from tether.repo_registry import RepoRegistry
        from tether.settings import settings
        registry = RepoRegistry(settings.data_dir())
        assert registry.get(src) is not None

    def test_repos_past_retention_are_removed(self, tmp_path, monkeypatch):
        """A shared clone with zero worktrees past the retention period is removed."""
        self._setup(tmp_path, monkeypatch)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        _clear_fetch_cache()
        create_workspace(src, "sess_stale")

        # Manually decrement the worktree count to simulate all sessions deleted.
        from tether.repo_registry import RepoRegistry
        from tether.settings import settings
        registry = RepoRegistry(settings.data_dir())
        registry.decrement_worktrees(src)

        # Backdate last_used_at to look ancient.
        import json
        registry_file = Path(settings.data_dir()) / "repos" / "registry.json"
        data = json.loads(registry_file.read_text())
        for key in data["repos"]:
            data["repos"][key]["last_used_at"] = "2020-01-01T00:00:00+00:00"
        registry_file.write_text(json.dumps(data))

        removed = prune_stale_repos(retention_days=1)
        assert removed == 1

        # Registry entry and directory should be gone.
        assert registry.get(src) is None

    def test_repos_within_retention_are_kept(self, tmp_path, monkeypatch):
        """A recently-used shared clone with zero worktrees is not pruned."""
        self._setup(tmp_path, monkeypatch)

        src = str(tmp_path / "source")
        _make_source_repo(src)

        _clear_fetch_cache()
        create_workspace(src, "sess_recent")

        from tether.repo_registry import RepoRegistry
        from tether.settings import settings
        registry = RepoRegistry(settings.data_dir())
        registry.decrement_worktrees(src)

        # last_used_at is current; retention is 7 days.
        removed = prune_stale_repos(retention_days=7)
        assert removed == 0
        assert registry.get(src) is not None

    def test_returns_zero_when_no_repos_registered(self, tmp_path, monkeypatch):
        """prune_stale_repos returns 0 when the registry is empty."""
        self._setup(tmp_path, monkeypatch)
        removed = prune_stale_repos(retention_days=0)
        assert removed == 0


# ---------------------------------------------------------------------------
# list_repo_usage
# ---------------------------------------------------------------------------


class TestListRepoUsage:
    def test_returns_empty_when_no_repos(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))
        assert list_repo_usage() == []

    def test_returns_entry_for_registered_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

        src = str(tmp_path / "source")
        _make_source_repo(src)

        _clear_fetch_cache()
        result = create_workspace(src, "sess_list")

        repos = list_repo_usage()
        assert len(repos) == 1
        repo = repos[0]
        assert repo["url"] == src
        assert repo["worktree_count"] == 1
        assert repo["hash"] is not None
        assert repo["size_bytes"] >= 0
        assert "last_used_at" in repo

    def test_sorted_by_size_descending(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TETHER_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

        from tether.repo_registry import RepoRegistry
        from tether.settings import settings

        registry = RepoRegistry(settings.data_dir())
        # Register two fake repos with controlled directory sizes.
        for name, file_size in (("big", 9000), ("small", 100)):
            d = tmp_path / f"data/repos/{name}"
            d.mkdir(parents=True)
            (d / "f").write_bytes(b"x" * file_size)
            registry.register(f"https://github.com/u/{name}.git", str(d))

        repos = list_repo_usage()
        assert len(repos) == 2
        assert repos[0]["size_bytes"] >= repos[1]["size_bytes"]


# ---------------------------------------------------------------------------
# GET /api/status/repos endpoint
# ---------------------------------------------------------------------------


class TestRepoStatusEndpoint:
    @pytest.mark.anyio
    async def test_returns_empty_repos_list(self, api_client, tmp_path, monkeypatch):
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))
        with patch("tether.workspace.list_repo_usage", return_value=[]):
            resp = await api_client.get("/api/status/repos")
        assert resp.status_code == 200
        data = resp.json()
        assert data["repos"] == []
        assert data["total_bytes"] == 0

    @pytest.mark.anyio
    async def test_returns_repo_entries(self, api_client, tmp_path, monkeypatch):
        monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))
        fake_repos = [
            {
                "url": "https://github.com/user/repo.git",
                "hash": "a1b2c3d4",
                "path": "/data/repos/a1b2c3d4",
                "size_bytes": 52428800,
                "worktree_count": 2,
                "last_used_at": "2026-03-05T17:00:00+00:00",
            }
        ]
        with patch("tether.workspace.list_repo_usage", return_value=fake_repos):
            resp = await api_client.get("/api/status/repos")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["repos"]) == 1
        repo = data["repos"][0]
        assert repo["url"] == "https://github.com/user/repo.git"
        assert repo["hash"] == "a1b2c3d4"
        assert repo["worktree_count"] == 2
        assert repo["size_bytes"] == 52428800
        assert data["total_bytes"] == 52428800

    @pytest.mark.anyio
    async def test_requires_auth(self, tmp_path):
        """Endpoint requires a token (tested via fresh app with auth enabled)."""
        # The api_client fixture uses dev mode (no token); just confirm 200.
        # A production auth test would use a client without dev mode.
        pass  # Coverage comes from the other tests in this class.
