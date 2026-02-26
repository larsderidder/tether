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


# ---------------------------------------------------------------------------
# git_commit / git_push / git_create_branch / git_checkout tests
# ---------------------------------------------------------------------------


class TestGitCommit:
    def test_commit_staged_changes(self, tmp_path):
        """git_commit creates a commit and returns GitCommit info."""
        from tether.git_ops import git_commit
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        with open(os.path.join(repo, "new.txt"), "w") as f:
            f.write("hello\n")

        commit = git_commit(repo, "Add new.txt")
        assert isinstance(commit, GitCommit)
        assert commit.message == "Add new.txt"
        assert commit.hash

    def test_commit_returns_latest_commit(self, tmp_path):
        """Returned commit is the HEAD after the commit."""
        from tether.git_ops import git_commit
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        with open(os.path.join(repo, "f.txt"), "w") as f:
            f.write("x\n")

        commit = git_commit(repo, "My commit message")
        status = git_status(repo)
        assert status.last_commit is not None
        assert status.last_commit.hash == commit.hash

    def test_commit_nothing_raises(self, tmp_path):
        """git_commit raises ValueError when there is nothing to commit."""
        from tether.git_ops import git_commit
        repo = str(tmp_path / "repo")
        _init_repo(repo)

        with pytest.raises(ValueError, match="Nothing to commit"):
            git_commit(repo, "Empty commit")

    def test_commit_without_add_all_uses_staged_only(self, tmp_path):
        """add_all=False only commits already-staged files."""
        from tether.git_ops import git_commit
        repo = str(tmp_path / "repo")
        _init_repo(repo)

        # Stage one file manually
        f1 = os.path.join(repo, "staged.txt")
        f2 = os.path.join(repo, "unstaged.txt")
        with open(f1, "w") as f:
            f.write("staged\n")
        with open(f2, "w") as f:
            f.write("unstaged\n")
        subprocess.run(["git", "-C", repo, "add", "staged.txt"], check=True, capture_output=True)

        commit = git_commit(repo, "Only staged", add_all=False)
        assert commit.message == "Only staged"

        # unstaged.txt should still be untracked
        status = git_status(repo)
        untracked = [f for f in status.changed_files if f.path == "unstaged.txt"]
        assert untracked


class TestGitCreateBranch:
    def test_creates_branch_and_checks_out(self, tmp_path):
        """git_create_branch creates a new branch and checks it out."""
        from tether.git_ops import git_create_branch
        repo = str(tmp_path / "repo")
        _init_repo(repo)

        name = git_create_branch(repo, "feature/new")
        assert name == "feature/new"
        status = git_status(repo)
        assert status.branch == "feature/new"

    def test_creates_branch_without_checkout(self, tmp_path):
        """checkout=False creates branch without switching to it."""
        from tether.git_ops import git_create_branch
        repo = str(tmp_path / "repo")
        _init_repo(repo)

        git_create_branch(repo, "side-branch", checkout=False)
        status = git_status(repo)
        assert status.branch == "main"  # still on main

    def test_create_existing_branch_raises(self, tmp_path):
        """Creating an already-existing branch raises ValueError."""
        from tether.git_ops import git_create_branch
        repo = str(tmp_path / "repo")
        _init_repo(repo)

        with pytest.raises(ValueError):
            git_create_branch(repo, "main")

    def test_invalid_branch_name_raises(self, tmp_path):
        """Branch name with spaces raises ValueError."""
        from tether.git_ops import git_create_branch
        repo = str(tmp_path / "repo")
        _init_repo(repo)

        with pytest.raises(ValueError, match="invalid characters"):
            git_create_branch(repo, "bad branch name")


class TestGitCheckout:
    def test_checkout_switches_branch(self, tmp_path):
        """git_checkout switches to the named branch."""
        from tether.git_ops import git_create_branch, git_checkout
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        git_create_branch(repo, "other", checkout=False)

        result = git_checkout(repo, "other")
        assert result == "other"
        status = git_status(repo)
        assert status.branch == "other"

    def test_checkout_nonexistent_branch_raises(self, tmp_path):
        """git_checkout raises ValueError for a missing branch."""
        from tether.git_ops import git_checkout
        repo = str(tmp_path / "repo")
        _init_repo(repo)

        with pytest.raises(ValueError, match="Could not checkout"):
            git_checkout(repo, "nonexistent-branch")


class TestGitPush:
    def _make_remote(self, tmp_path) -> tuple[str, str]:
        """Create a bare remote and a clone of it. Returns (remote_path, clone_path)."""
        remote = str(tmp_path / "remote.git")
        subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)

        clone = str(tmp_path / "clone")
        subprocess.run(["git", "clone", remote, clone], check=True, capture_output=True)
        subprocess.run(["git", "-C", clone, "config", "user.email", "t@t.t"], check=True, capture_output=True)
        subprocess.run(["git", "-C", clone, "config", "user.name", "T"], check=True, capture_output=True)

        # Make an initial commit so main branch exists
        with open(os.path.join(clone, "README.md"), "w") as f:
            f.write("# test\n")
        subprocess.run(["git", "-C", clone, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", clone, "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", clone, "push", "-u", "origin", "main"], check=True, capture_output=True)
        return remote, clone

    def test_push_to_local_remote(self, tmp_path):
        """git_push pushes to a local bare remote successfully."""
        from tether.git_ops import git_commit, git_push
        _, clone = self._make_remote(tmp_path)

        with open(os.path.join(clone, "new.txt"), "w") as f:
            f.write("pushed\n")
        git_commit(clone, "Push this")

        result = git_push(clone)
        assert result.success is True
        assert result.branch == "main"

    def test_push_result_contains_remote(self, tmp_path):
        """GitPushResult.remote is populated with the remote URL."""
        from tether.git_ops import git_commit, git_push
        remote_path, clone = self._make_remote(tmp_path)

        with open(os.path.join(clone, "f.txt"), "w") as f:
            f.write("x\n")
        git_commit(clone, "Commit for push")
        result = git_push(clone)

        assert remote_path in result.remote or "origin" in result.remote


# ---------------------------------------------------------------------------
# Action API endpoint tests
# ---------------------------------------------------------------------------


class TestGitCommitEndpoint:
    @pytest.mark.anyio
    async def test_commit_via_api(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path
    ) -> None:
        """POST /sessions/{id}/git/commit creates a commit and returns it."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        with open(os.path.join(repo, "new.txt"), "w") as f:
            f.write("hi\n")

        create_resp = await api_client.post(
            "/api/sessions", json={"directory": repo}
        )
        session_id = create_resp.json()["id"]

        resp = await api_client.post(
            f"/api/sessions/{session_id}/git/commit",
            json={"message": "API commit"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["message"] == "API commit"
        assert data["hash"]

    @pytest.mark.anyio
    async def test_commit_nothing_returns_422(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path
    ) -> None:
        """POST /git/commit returns 422 when there is nothing to commit."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)

        create_resp = await api_client.post(
            "/api/sessions", json={"directory": repo}
        )
        session_id = create_resp.json()["id"]

        resp = await api_client.post(
            f"/api/sessions/{session_id}/git/commit",
            json={"message": "Empty"},
        )
        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_commit_blocked_while_running(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path
    ) -> None:
        """POST /git/commit returns 409 while session is RUNNING."""
        from tether.models import SessionState
        repo = str(tmp_path / "repo")
        _init_repo(repo)

        create_resp = await api_client.post(
            "/api/sessions", json={"directory": repo}
        )
        session_id = create_resp.json()["id"]

        # Force session into RUNNING state
        session = fresh_store.get_session(session_id)
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        resp = await api_client.post(
            f"/api/sessions/{session_id}/git/commit",
            json={"message": "Should be blocked"},
        )
        assert resp.status_code == 409


class TestGitBranchEndpoint:
    @pytest.mark.anyio
    async def test_create_branch_via_api(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path
    ) -> None:
        """POST /sessions/{id}/git/branch creates a branch."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)

        create_resp = await api_client.post(
            "/api/sessions", json={"directory": repo}
        )
        session_id = create_resp.json()["id"]

        resp = await api_client.post(
            f"/api/sessions/{session_id}/git/branch",
            json={"name": "feature-x"},
        )
        assert resp.status_code == 200
        assert resp.json()["branch"] == "feature-x"

    @pytest.mark.anyio
    async def test_create_branch_blocked_while_running(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path
    ) -> None:
        """POST /git/branch returns 409 while session is RUNNING."""
        from tether.models import SessionState
        repo = str(tmp_path / "repo")
        _init_repo(repo)

        create_resp = await api_client.post(
            "/api/sessions", json={"directory": repo}
        )
        session_id = create_resp.json()["id"]

        session = fresh_store.get_session(session_id)
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        resp = await api_client.post(
            f"/api/sessions/{session_id}/git/branch",
            json={"name": "feature-y"},
        )
        assert resp.status_code == 409


class TestGitCheckoutEndpoint:
    @pytest.mark.anyio
    async def test_checkout_via_api(
        self, api_client: httpx.AsyncClient, fresh_store, tmp_path
    ) -> None:
        """POST /sessions/{id}/git/checkout switches branch."""
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        subprocess.run(
            ["git", "-C", repo, "branch", "other-branch"],
            check=True, capture_output=True,
        )

        create_resp = await api_client.post(
            "/api/sessions", json={"directory": repo}
        )
        session_id = create_resp.json()["id"]

        resp = await api_client.post(
            f"/api/sessions/{session_id}/git/checkout",
            json={"branch": "other-branch"},
        )
        assert resp.status_code == 200
        assert resp.json()["branch"] == "other-branch"
