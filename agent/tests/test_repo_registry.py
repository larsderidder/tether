"""Tests for the repo registry module."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from tether.repo_registry import (
    RepoEntry,
    RepoRegistry,
    normalize_repo_url,
    repo_url_hash,
)


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------


class TestNormalizeRepoUrl:
    def test_strips_https_scheme(self):
        assert normalize_repo_url("https://github.com/user/repo") == "github.com/user/repo"

    def test_strips_http_scheme(self):
        assert normalize_repo_url("http://github.com/user/repo") == "github.com/user/repo"

    def test_strips_git_scheme(self):
        assert normalize_repo_url("git://github.com/user/repo") == "github.com/user/repo"

    def test_strips_ssh_scheme(self):
        assert normalize_repo_url("ssh://git@github.com/user/repo") == "github.com/user/repo"

    def test_converts_scp_syntax(self):
        assert normalize_repo_url("git@github.com:user/repo") == "github.com/user/repo"

    def test_strips_trailing_dot_git(self):
        assert normalize_repo_url("https://github.com/user/repo.git") == "github.com/user/repo"

    def test_strips_trailing_slash(self):
        assert normalize_repo_url("https://github.com/user/repo/") == "github.com/user/repo"

    def test_lowercases_hostname(self):
        assert normalize_repo_url("https://GitHub.COM/user/repo") == "github.com/user/repo"

    def test_scp_ssh_and_https_produce_same_result(self):
        ssh = normalize_repo_url("git@github.com:user/repo.git")
        https = normalize_repo_url("https://github.com/user/repo.git")
        assert ssh == https

    def test_strips_leading_trailing_whitespace(self):
        assert normalize_repo_url("  https://github.com/user/repo.git  ") == "github.com/user/repo"

    def test_preserves_path_case(self):
        # Only the hostname is lowercased; path casing is preserved.
        result = normalize_repo_url("https://github.com/MyOrg/MyRepo")
        assert result == "github.com/MyOrg/MyRepo"

    def test_scp_with_org_and_repo(self):
        assert normalize_repo_url("git@gitlab.com:myorg/myrepo.git") == "gitlab.com/myorg/myrepo"


class TestRepoUrlHash:
    def test_returns_8_hex_chars(self):
        h = repo_url_hash("https://github.com/user/repo.git")
        assert len(h) == 8
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_url_same_hash(self):
        h1 = repo_url_hash("https://github.com/user/repo.git")
        h2 = repo_url_hash("https://github.com/user/repo.git")
        assert h1 == h2

    def test_ssh_and_https_produce_same_hash(self):
        h_ssh = repo_url_hash("git@github.com:user/repo.git")
        h_https = repo_url_hash("https://github.com/user/repo.git")
        assert h_ssh == h_https

    def test_different_repos_different_hash(self):
        h1 = repo_url_hash("https://github.com/user/repo-a.git")
        h2 = repo_url_hash("https://github.com/user/repo-b.git")
        assert h1 != h2


# ---------------------------------------------------------------------------
# RepoRegistry: basic CRUD
# ---------------------------------------------------------------------------


class TestRepoRegistryRegister:
    def test_register_new_repo(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))
        path = str(tmp_path / "repos" / "a1b2c3d4")

        entry = registry.register("https://github.com/user/repo.git", path)

        assert isinstance(entry, RepoEntry)
        assert entry.url == "https://github.com/user/repo.git"
        assert entry.path == path
        assert entry.worktree_count == 0
        assert entry.created_at
        assert entry.last_used_at

    def test_register_duplicate_raises(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))
        path = str(tmp_path / "clone")

        registry.register("https://github.com/user/repo.git", path)

        with pytest.raises(ValueError, match="already registered"):
            registry.register("https://github.com/user/repo.git", path)

    def test_register_duplicate_via_ssh_url_raises(self, tmp_path):
        """Registering the SSH form after the HTTPS form is a duplicate."""
        registry = RepoRegistry(str(tmp_path))
        path = str(tmp_path / "clone")

        registry.register("https://github.com/user/repo.git", path)

        with pytest.raises(ValueError, match="already registered"):
            registry.register("git@github.com:user/repo.git", path)

    def test_registry_file_created(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))
        path = str(tmp_path / "clone")

        registry.register("https://github.com/user/repo.git", path)

        registry_file = tmp_path / "repos" / "registry.json"
        assert registry_file.exists()


class TestRepoRegistryGet:
    def test_get_returns_entry(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))
        path = str(tmp_path / "clone")
        registry.register("https://github.com/user/repo.git", path)

        entry = registry.get("https://github.com/user/repo.git")

        assert entry is not None
        assert entry.url == "https://github.com/user/repo.git"

    def test_get_by_ssh_url_after_https_register(self, tmp_path):
        """Lookup by SSH URL finds an entry registered via HTTPS URL."""
        registry = RepoRegistry(str(tmp_path))
        path = str(tmp_path / "clone")
        registry.register("https://github.com/user/repo.git", path)

        entry = registry.get("git@github.com:user/repo.git")

        assert entry is not None
        assert entry.path == path

    def test_get_returns_none_for_unknown_url(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))

        assert registry.get("https://github.com/nobody/norepo.git") is None

    def test_get_returns_none_on_empty_registry(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))

        assert registry.get("https://github.com/user/repo.git") is None


# ---------------------------------------------------------------------------
# Worktree count
# ---------------------------------------------------------------------------


class TestWorktreeCount:
    def test_increment_worktrees(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))
        path = str(tmp_path / "clone")
        registry.register("https://github.com/user/repo.git", path)

        registry.increment_worktrees("https://github.com/user/repo.git")
        registry.increment_worktrees("https://github.com/user/repo.git")

        entry = registry.get("https://github.com/user/repo.git")
        assert entry.worktree_count == 2

    def test_decrement_worktrees(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))
        path = str(tmp_path / "clone")
        registry.register("https://github.com/user/repo.git", path)
        registry.increment_worktrees("https://github.com/user/repo.git")
        registry.increment_worktrees("https://github.com/user/repo.git")

        registry.decrement_worktrees("https://github.com/user/repo.git")

        entry = registry.get("https://github.com/user/repo.git")
        assert entry.worktree_count == 1

    def test_decrement_does_not_go_negative(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))
        path = str(tmp_path / "clone")
        registry.register("https://github.com/user/repo.git", path)

        registry.decrement_worktrees("https://github.com/user/repo.git")

        entry = registry.get("https://github.com/user/repo.git")
        assert entry.worktree_count == 0

    def test_increment_unknown_url_raises(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))

        with pytest.raises(KeyError):
            registry.increment_worktrees("https://github.com/nobody/norepo.git")

    def test_decrement_unknown_url_raises(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))

        with pytest.raises(KeyError):
            registry.decrement_worktrees("https://github.com/nobody/norepo.git")

    def test_decrement_by_path(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))
        clone_path = str(tmp_path / "repos" / "a1b2c3d4")
        Path(clone_path).mkdir(parents=True)

        registry.register("https://github.com/user/repo.git", clone_path)
        registry.increment_worktrees("https://github.com/user/repo.git")

        registry.decrement_worktrees_by_path(clone_path)

        entry = registry.get("https://github.com/user/repo.git")
        assert entry.worktree_count == 0

    def test_decrement_by_path_noop_for_unknown_path(self, tmp_path):
        """decrement_worktrees_by_path is a no-op for an unregistered path."""
        registry = RepoRegistry(str(tmp_path))
        # Must not raise.
        registry.decrement_worktrees_by_path("/some/random/path")


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------


class TestRepoRegistryRemove:
    def test_remove_unregisters_repo(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))
        path = str(tmp_path / "clone")
        registry.register("https://github.com/user/repo.git", path)

        registry.remove("https://github.com/user/repo.git")

        assert registry.get("https://github.com/user/repo.git") is None

    def test_remove_deletes_directory(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        (clone_path / "file.txt").write_text("content")

        registry.register("https://github.com/user/repo.git", str(clone_path))
        registry.remove("https://github.com/user/repo.git")

        assert not clone_path.exists()

    def test_remove_unknown_url_is_noop(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))
        # Must not raise.
        registry.remove("https://github.com/nobody/norepo.git")

    def test_remove_tolerates_missing_directory(self, tmp_path):
        """remove() does not fail if the clone dir was already deleted."""
        registry = RepoRegistry(str(tmp_path))
        clone_path = str(tmp_path / "already_gone")

        registry.register("https://github.com/user/repo.git", clone_path)
        # Do not create the directory; remove() must still succeed.
        registry.remove("https://github.com/user/repo.git")

        assert registry.get("https://github.com/user/repo.git") is None


# ---------------------------------------------------------------------------
# list_repos
# ---------------------------------------------------------------------------


class TestListRepos:
    def test_list_empty(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))
        assert registry.list_repos() == []

    def test_list_returns_all(self, tmp_path):
        registry = RepoRegistry(str(tmp_path))
        registry.register("https://github.com/user/repo-a.git", str(tmp_path / "a"))
        registry.register("https://github.com/user/repo-b.git", str(tmp_path / "b"))

        entries = registry.list_repos()

        assert len(entries) == 2
        urls = {e.url for e in entries}
        assert "https://github.com/user/repo-a.git" in urls
        assert "https://github.com/user/repo-b.git" in urls


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_registry_survives_reload(self, tmp_path):
        """Data written by one instance is readable by a new instance."""
        registry1 = RepoRegistry(str(tmp_path))
        path = str(tmp_path / "clone")
        registry1.register("https://github.com/user/repo.git", path)
        registry1.increment_worktrees("https://github.com/user/repo.git")

        registry2 = RepoRegistry(str(tmp_path))
        entry = registry2.get("https://github.com/user/repo.git")

        assert entry is not None
        assert entry.worktree_count == 1

    def test_empty_registry_file_handled_gracefully(self, tmp_path):
        """A corrupt or empty registry.json does not crash the registry."""
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        (repos_dir / "registry.json").write_text("")

        registry = RepoRegistry(str(tmp_path))
        assert registry.list_repos() == []


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_register_no_data_race(self, tmp_path):
        """Concurrent registrations for different URLs all persist correctly."""
        registry = RepoRegistry(str(tmp_path))
        urls = [f"https://github.com/user/repo-{i}.git" for i in range(20)]
        errors: list[Exception] = []

        def register(url: str) -> None:
            try:
                registry.register(url, str(tmp_path / f"clone-{url[-6:]}"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=register, args=(u,)) for u in urls]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Errors during concurrent registration: {errors}"
        entries = registry.list_repos()
        assert len(entries) == len(urls)

    def test_concurrent_increment_is_safe(self, tmp_path):
        """Concurrent increments produce a consistent final count."""
        registry = RepoRegistry(str(tmp_path))
        registry.register("https://github.com/user/repo.git", str(tmp_path / "clone"))
        n = 10

        def increment() -> None:
            registry.increment_worktrees("https://github.com/user/repo.git")

        threads = [threading.Thread(target=increment) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        entry = registry.get("https://github.com/user/repo.git")
        assert entry.worktree_count == n
