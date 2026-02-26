"""Tests for git_ops module and the git API endpoints."""

from __future__ import annotations

import os
import subprocess

import pytest
import httpx

from tether.git_ops import git_status, git_log, GitStatus, GitCommit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(path: str) -> None:
    """Initialise a git repo with a committed README on branch 'main'."""
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
        f.write("# test\n")
    subprocess.run(["git", "-C", path, "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", path, "commit", "-m", "Initial commit"],
        check=True, capture_output=True,
    )


def _add_commit(path: str, filename: str, message: str) -> None:
    """Add a file and commit it."""
    fpath = os.path.join(path, filename)
    with open(fpath, "w") as f:
        f.write(f"content of {filename}\n")
    subprocess.run(["git", "-C", path, "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", path, "commit", "-m", message],
        check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# git_status tests
# ---------------------------------------------------------------------------


class TestGitStatus:
    def test_returns_git_status(self, tmp_path):
        """git_status returns a GitStatus for a valid repo."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        status = git_status(repo)
        assert isinstance(status, GitStatus)

    def test_branch_name(self, tmp_path):
        """branch field reflects the current branch."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        status = git_status(repo)
        assert status.branch == "main"

    def test_clean_repo_not_dirty(self, tmp_path):
        """A clean repo has dirty=False and zero change counts."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        status = git_status(repo)
        assert status.dirty is False
        assert status.staged_count == 0
        assert status.unstaged_count == 0
        assert status.untracked_count == 0

    def test_untracked_file_makes_dirty(self, tmp_path):
        """An untracked file sets dirty=True and increments untracked_count."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        with open(os.path.join(repo, "new.txt"), "w") as f:
            f.write("hi\n")
        status = git_status(repo)
        assert status.dirty is True
        assert status.untracked_count == 1

    def test_staged_file_detected(self, tmp_path):
        """A staged file increments staged_count."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        with open(os.path.join(repo, "staged.txt"), "w") as f:
            f.write("staged\n")
        subprocess.run(
            ["git", "-C", repo, "add", "staged.txt"], check=True, capture_output=True
        )
        status = git_status(repo)
        assert status.staged_count == 1

    def test_modified_unstaged_file_detected(self, tmp_path):
        """A modified but unstaged file increments unstaged_count."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        readme = os.path.join(repo, "README.md")
        with open(readme, "a") as f:
            f.write("extra line\n")
        status = git_status(repo)
        assert status.unstaged_count == 1
        assert status.dirty is True

    def test_last_commit_populated(self, tmp_path):
        """last_commit is populated with hash, message, author, timestamp."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        status = git_status(repo)
        assert status.last_commit is not None
        assert status.last_commit.hash != ""
        assert status.last_commit.message == "Initial commit"
        assert status.last_commit.author == "Test"
        assert status.last_commit.timestamp != ""

    def test_no_remote(self, tmp_path):
        """A repo with no remote has None for remote_url and remote_branch."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        status = git_status(repo)
        assert status.remote_url is None
        assert status.remote_branch is None
        assert status.ahead == 0
        assert status.behind == 0

    def test_raises_for_non_repo(self, tmp_path):
        """git_status raises ValueError for a non-repo directory."""
        not_a_repo = str(tmp_path / "plain")
        os.makedirs(not_a_repo)
        with pytest.raises(ValueError, match="Not a git repository"):
            git_status(not_a_repo)

    def test_changed_files_contain_paths(self, tmp_path):
        """changed_files lists files with correct path and status."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        with open(os.path.join(repo, "new.txt"), "w") as f:
            f.write("hi\n")
        status = git_status(repo)
        paths = [f.path for f in status.changed_files]
        assert "new.txt" in paths
        untracked = [f for f in status.changed_files if f.path == "new.txt"]
        assert untracked[0].status == "untracked"
        assert untracked[0].staged is False


# ---------------------------------------------------------------------------
# git_log tests
# ---------------------------------------------------------------------------


class TestGitLog:
    def test_returns_commits(self, tmp_path):
        """git_log returns a list of GitCommit objects."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        commits = git_log(repo)
        assert len(commits) >= 1
        assert isinstance(commits[0], GitCommit)

    def test_commit_fields_populated(self, tmp_path):
        """Each commit has non-empty hash, message, author, timestamp."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        commits = git_log(repo)
        c = commits[0]
        assert c.hash
        assert c.message == "Initial commit"
        assert c.author == "Test"
        assert c.timestamp

    def test_count_limit_respected(self, tmp_path):
        """count parameter limits the number of commits returned."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        for i in range(5):
            _add_commit(repo, f"file{i}.txt", f"Commit {i}")
        all_commits = git_log(repo, count=10)
        limited = git_log(repo, count=3)
        assert len(limited) == 3
        assert len(all_commits) == 6  # initial + 5

    def test_newest_first(self, tmp_path):
        """Commits are returned newest first."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        _add_commit(repo, "second.txt", "Second commit")
        commits = git_log(repo, count=2)
        assert commits[0].message == "Second commit"
        assert commits[1].message == "Initial commit"

    def test_empty_repo_returns_empty(self, tmp_path):
        """A repo with no commits returns an empty list."""
        repo = str(tmp_path / "empty_repo")
        subprocess.run(["git", "init", "-b", "main", repo], check=True, capture_output=True)
        commits = git_log(repo)
        assert commits == []

    def test_raises_for_non_repo(self, tmp_path):
        """git_log raises ValueError for a non-repo directory."""
        not_a_repo = str(tmp_path / "plain")
        os.makedirs(not_a_repo)
        with pytest.raises(ValueError):
            git_log(not_a_repo)


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestGitStatusEndpoint:
    @pytest.mark.anyio
    async def test_git_status_for_session_with_repo(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path
    ) -> None:
        """GET /sessions/{id}/git returns status for a session with a git repo."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)

        create_resp = await api_client.post(
            "/api/sessions", json={"directory": repo}
        )
        assert create_resp.status_code == 201
        session_id = create_resp.json()["id"]

        resp = await api_client.get(f"/api/sessions/{session_id}/git")
        assert resp.status_code == 200
        data = resp.json()
        assert data["branch"] == "main"
        assert data["dirty"] is False
        assert "last_commit" in data
        assert data["last_commit"]["message"] == "Initial commit"

    @pytest.mark.anyio
    async def test_git_status_404_for_unknown_session(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """GET /sessions/{id}/git returns 404 for unknown session."""
        resp = await api_client.get("/api/sessions/sess_nonexistent/git")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_git_status_422_for_session_without_repo(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path
    ) -> None:
        """GET /sessions/{id}/git returns 422 if directory has no git repo."""
        plain_dir = str(tmp_path / "plain")
        os.makedirs(plain_dir)

        create_resp = await api_client.post(
            "/api/sessions", json={"directory": plain_dir}
        )
        session_id = create_resp.json()["id"]

        resp = await api_client.get(f"/api/sessions/{session_id}/git")
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_git_status_reflects_dirty_state(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path
    ) -> None:
        """dirty=True is returned when the working tree has changes."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        with open(os.path.join(repo, "new.txt"), "w") as f:
            f.write("hi\n")

        create_resp = await api_client.post(
            "/api/sessions", json={"directory": repo}
        )
        session_id = create_resp.json()["id"]

        resp = await api_client.get(f"/api/sessions/{session_id}/git")
        assert resp.status_code == 200
        assert resp.json()["dirty"] is True


class TestGitLogEndpoint:
    @pytest.mark.anyio
    async def test_git_log_returns_commits(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path
    ) -> None:
        """GET /sessions/{id}/git/log returns a list of commits."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        _add_commit(repo, "second.txt", "Second commit")

        create_resp = await api_client.post(
            "/api/sessions", json={"directory": repo}
        )
        session_id = create_resp.json()["id"]

        resp = await api_client.get(f"/api/sessions/{session_id}/git/log")
        assert resp.status_code == 200
        commits = resp.json()
        assert len(commits) == 2
        assert commits[0]["message"] == "Second commit"

    @pytest.mark.anyio
    async def test_git_log_count_query_param(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path
    ) -> None:
        """count query param limits number of commits returned."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        for i in range(5):
            _add_commit(repo, f"f{i}.txt", f"Commit {i}")

        create_resp = await api_client.post(
            "/api/sessions", json={"directory": repo}
        )
        session_id = create_resp.json()["id"]

        resp = await api_client.get(
            f"/api/sessions/{session_id}/git/log?count=3"
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    @pytest.mark.anyio
    async def test_git_log_404_for_unknown_session(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """GET /sessions/{id}/git/log returns 404 for unknown session."""
        resp = await api_client.get("/api/sessions/sess_nonexistent/git/log")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_git_log_422_for_session_without_repo(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path
    ) -> None:
        """GET /sessions/{id}/git/log returns 422 if directory has no git repo."""
        plain_dir = str(tmp_path / "plain")
        os.makedirs(plain_dir)

        create_resp = await api_client.post(
            "/api/sessions", json={"directory": plain_dir}
        )
        session_id = create_resp.json()["id"]

        resp = await api_client.get(f"/api/sessions/{session_id}/git/log")
        assert resp.status_code == 422
