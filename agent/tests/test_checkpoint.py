"""Tests for the auto-checkpoint feature (git commit after each agent turn)."""

from __future__ import annotations

import os
import subprocess

import pytest

from tether.models import SessionState
from tether.store import SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(path: str) -> None:
    """Create a committed git repo at *path*."""
    subprocess.run(["git", "init", "-b", "main", path], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", path, "config", "user.email", "t@t.t"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", path, "config", "user.name", "T"],
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


def _add_untracked_file(repo: str, name: str = "change.txt") -> None:
    with open(os.path.join(repo, name), "w") as f:
        f.write("change\n")


def _collected_events(fresh_store: SessionStore, session_id: str) -> list[dict]:
    """Read all events from the event log for a session."""
    return fresh_store.read_event_log(session_id, since_seq=0)


# ---------------------------------------------------------------------------
# Unit tests for _maybe_checkpoint
# ---------------------------------------------------------------------------


class TestMaybeCheckpoint:
    @pytest.mark.anyio
    async def test_no_checkpoint_when_disabled(
        self, fresh_store: SessionStore, tmp_path, monkeypatch
    ) -> None:
        """No commit is made when TETHER_GIT_AUTO_CHECKPOINT is off."""
        monkeypatch.setenv("TETHER_GIT_AUTO_CHECKPOINT", "0")
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        _add_untracked_file(repo)

        session = fresh_store.create_session("test", "main")
        fresh_store.set_workdir(session.id, repo, managed=False)
        session = fresh_store.get_session(session.id)

        from tether.api.runner_events import _maybe_checkpoint
        from tether.git_ops import git_log

        commits_before = len(git_log(repo))
        await _maybe_checkpoint(session.id)
        commits_after = len(git_log(repo))

        assert commits_after == commits_before

    @pytest.mark.anyio
    async def test_no_checkpoint_when_clean(
        self, fresh_store: SessionStore, tmp_path, monkeypatch
    ) -> None:
        """No commit is made when the working tree is clean."""
        monkeypatch.setenv("TETHER_GIT_AUTO_CHECKPOINT", "1")
        repo = str(tmp_path / "repo")
        _init_repo(repo)  # clean, nothing to commit

        session = fresh_store.create_session("test", "main")
        fresh_store.set_workdir(session.id, repo, managed=False)
        session = fresh_store.get_session(session.id)

        from tether.api.runner_events import _maybe_checkpoint
        from tether.git_ops import git_log

        commits_before = len(git_log(repo))
        await _maybe_checkpoint(session.id)
        commits_after = len(git_log(repo))

        assert commits_after == commits_before

    @pytest.mark.anyio
    async def test_checkpoint_commits_dirty_tree(
        self, fresh_store: SessionStore, tmp_path, monkeypatch
    ) -> None:
        """A new commit is created when there are uncommitted changes."""
        monkeypatch.setenv("TETHER_GIT_AUTO_CHECKPOINT", "1")
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        _add_untracked_file(repo)

        session = fresh_store.create_session("test", "main")
        fresh_store.set_workdir(session.id, repo, managed=False)
        session = fresh_store.get_session(session.id)

        from tether.api.runner_events import _maybe_checkpoint
        from tether.git_ops import git_log

        commits_before = len(git_log(repo))
        await _maybe_checkpoint(session.id)
        commits_after = len(git_log(repo))

        assert commits_after == commits_before + 1

    @pytest.mark.anyio
    async def test_checkpoint_message_contains_turn_number(
        self, fresh_store: SessionStore, tmp_path, monkeypatch
    ) -> None:
        """Commit message contains '[tether] checkpoint after turn N'."""
        monkeypatch.setenv("TETHER_GIT_AUTO_CHECKPOINT", "1")
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        _add_untracked_file(repo, "f1.txt")

        session = fresh_store.create_session("test", "main")
        fresh_store.set_workdir(session.id, repo, managed=False)

        from tether.api.runner_events import _maybe_checkpoint
        from tether.git_ops import git_log

        await _maybe_checkpoint(session.id)
        commits = git_log(repo)
        assert "[tether] checkpoint after turn 1" in commits[0].message

    @pytest.mark.anyio
    async def test_checkpoint_turn_number_increments(
        self, fresh_store: SessionStore, tmp_path, monkeypatch
    ) -> None:
        """Turn number increments on each checkpoint call."""
        monkeypatch.setenv("TETHER_GIT_AUTO_CHECKPOINT", "1")
        repo = str(tmp_path / "repo")
        _init_repo(repo)

        session = fresh_store.create_session("test", "main")
        fresh_store.set_workdir(session.id, repo, managed=False)

        from tether.api.runner_events import _maybe_checkpoint
        from tether.git_ops import git_log

        # First turn
        _add_untracked_file(repo, "turn1.txt")
        await _maybe_checkpoint(session.id)

        # Second turn
        _add_untracked_file(repo, "turn2.txt")
        await _maybe_checkpoint(session.id)

        commits = git_log(repo, count=2)
        assert "turn 2" in commits[0].message
        assert "turn 1" in commits[1].message

    @pytest.mark.anyio
    async def test_checkpoint_emits_checkpoint_event(
        self, fresh_store: SessionStore, tmp_path, monkeypatch
    ) -> None:
        """A 'checkpoint' SSE event is emitted after a successful commit."""
        monkeypatch.setenv("TETHER_GIT_AUTO_CHECKPOINT", "1")
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        _add_untracked_file(repo)

        session = fresh_store.create_session("test", "main")
        fresh_store.set_workdir(session.id, repo, managed=False)
        session = fresh_store.get_session(session.id)

        from tether.api.runner_events import _maybe_checkpoint

        await _maybe_checkpoint(session.id)

        events = _collected_events(fresh_store, session.id)
        checkpoint_events = [e for e in events if e.get("type") == "checkpoint"]
        assert len(checkpoint_events) == 1
        data = checkpoint_events[0]["data"]
        assert data["commit_hash"]
        assert "[tether] checkpoint" in data["message"]

    @pytest.mark.anyio
    async def test_checkpoint_failure_emits_warning_not_exception(
        self, fresh_store: SessionStore, tmp_path, monkeypatch
    ) -> None:
        """A checkpoint failure emits a warning event and does not raise."""
        monkeypatch.setenv("TETHER_GIT_AUTO_CHECKPOINT", "1")
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        _add_untracked_file(repo)

        session = fresh_store.create_session("test", "main")
        fresh_store.set_workdir(session.id, repo, managed=False)
        session = fresh_store.get_session(session.id)

        from tether.api.runner_events import _maybe_checkpoint
        from unittest.mock import patch

        # Force git_commit to raise — patch at the source module level
        with patch("tether.git_ops.git_commit", side_effect=ValueError("forced failure")):
            await _maybe_checkpoint(session.id)  # must not raise

        events = _collected_events(fresh_store, session.id)
        warning_events = [e for e in events if e.get("type") == "warning"]
        assert any("checkpoint" in str(e).lower() for e in warning_events)

    @pytest.mark.anyio
    async def test_no_checkpoint_without_git_dir(
        self, fresh_store: SessionStore, tmp_path, monkeypatch
    ) -> None:
        """No checkpoint when session directory has no git repo."""
        monkeypatch.setenv("TETHER_GIT_AUTO_CHECKPOINT", "1")
        plain = str(tmp_path / "plain")
        os.makedirs(plain)

        session = fresh_store.create_session("test", "main")
        fresh_store.set_workdir(session.id, plain, managed=False)
        session = fresh_store.get_session(session.id)
        # directory_has_git should be False since no .git exists
        assert not session.directory_has_git

        from tether.api.runner_events import _maybe_checkpoint

        # Should be a no-op — no exception, no commits
        await _maybe_checkpoint(session.id)


# ---------------------------------------------------------------------------
# Integration: on_awaiting_input triggers checkpoint
# ---------------------------------------------------------------------------


class TestOnAwaitingInputCheckpoint:
    @pytest.mark.anyio
    async def test_on_awaiting_input_triggers_checkpoint(
        self, fresh_store: SessionStore, tmp_path, monkeypatch
    ) -> None:
        """on_awaiting_input auto-commits when TETHER_GIT_AUTO_CHECKPOINT=1."""
        monkeypatch.setenv("TETHER_GIT_AUTO_CHECKPOINT", "1")
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        _add_untracked_file(repo)

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)
        fresh_store.set_workdir(session.id, repo, managed=False)
        session = fresh_store.get_session(session.id)

        from tether.api.runner_events import ApiRunnerEvents
        from tether.git_ops import git_log

        commits_before = len(git_log(repo))
        events = ApiRunnerEvents()
        await events.on_awaiting_input(session.id)
        commits_after = len(git_log(repo))

        assert commits_after == commits_before + 1

    @pytest.mark.anyio
    async def test_on_awaiting_input_no_checkpoint_when_disabled(
        self, fresh_store: SessionStore, tmp_path, monkeypatch
    ) -> None:
        """on_awaiting_input does not commit when auto-checkpoint is off."""
        monkeypatch.setenv("TETHER_GIT_AUTO_CHECKPOINT", "0")
        repo = str(tmp_path / "repo")
        _init_repo(repo)
        _add_untracked_file(repo)

        session = fresh_store.create_session("test", "main")
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)
        fresh_store.set_workdir(session.id, repo, managed=False)

        from tether.api.runner_events import ApiRunnerEvents
        from tether.git_ops import git_log

        commits_before = len(git_log(repo))
        events = ApiRunnerEvents()
        await events.on_awaiting_input(session.id)
        commits_after = len(git_log(repo))

        assert commits_after == commits_before
