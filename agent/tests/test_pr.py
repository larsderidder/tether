"""Tests for PR/MR creation: detect_forge, create_pr, API endpoint, CLI."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tether.git_ops import (
    PrResult,
    _create_github_pr,
    _create_gitlab_mr,
    _extract_pr_number,
    _extract_url_from_output,
    create_pr,
    detect_forge,
)
from tether.store import SessionStore


# ---------------------------------------------------------------------------
# detect_forge
# ---------------------------------------------------------------------------


class TestDetectForge:
    def test_github_https(self):
        assert detect_forge("https://github.com/owner/repo.git") == "github"

    def test_github_ssh(self):
        assert detect_forge("git@github.com:owner/repo.git") == "github"

    def test_github_no_dotgit(self):
        assert detect_forge("https://github.com/owner/repo") == "github"

    def test_gitlab_https(self):
        assert detect_forge("https://gitlab.com/owner/repo.git") == "gitlab"

    def test_gitlab_ssh(self):
        assert detect_forge("git@gitlab.com:owner/repo.git") == "gitlab"

    def test_gitlab_self_hosted(self):
        assert detect_forge("https://gitlab.example.com/owner/repo.git") == "gitlab"

    def test_unknown_forge(self):
        assert detect_forge("https://bitbucket.org/owner/repo.git") is None

    def test_empty_string(self):
        assert detect_forge("") is None

    def test_case_insensitive(self):
        assert detect_forge("https://GITHUB.COM/owner/repo") == "github"


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


class TestExtractHelpers:
    def test_extract_pr_number_github(self):
        url = "https://github.com/owner/repo/pull/42"
        assert _extract_pr_number(url) == 42

    def test_extract_pr_number_gitlab(self):
        url = "https://gitlab.com/owner/repo/-/merge_requests/7"
        assert _extract_pr_number(url) == 7

    def test_extract_pr_number_missing(self):
        assert _extract_pr_number("https://example.com/no/number") == 0

    def test_extract_url_from_output(self):
        output = "Creating pull request...\nhttps://github.com/owner/repo/pull/5\nDone."
        assert _extract_url_from_output(output) == "https://github.com/owner/repo/pull/5"

    def test_extract_url_from_output_none(self):
        assert _extract_url_from_output("no url here") is None


# ---------------------------------------------------------------------------
# create_pr (unit, mocked subprocess)
# ---------------------------------------------------------------------------


def _make_git_repo(path: str) -> str:
    """Create a minimal git repo with one commit and a remote origin."""
    import os
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", path], check=True, capture_output=True)
    subprocess.run(["git", "-C", path, "config", "user.email", "t@t.t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", path, "config", "user.name", "T"], check=True, capture_output=True)
    with open(os.path.join(path, "README.md"), "w") as f:
        f.write("# test\n")
    subprocess.run(["git", "-C", path, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", path, "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", path, "remote", "add", "origin", "https://github.com/owner/repo.git"],
        check=True, capture_output=True,
    )
    return path


class TestCreateGithubPr:
    def test_creates_pr_with_correct_args(self, tmp_path):
        """create_pr calls gh pr create with correct arguments."""
        repo = _make_git_repo(str(tmp_path / "repo"))

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/pull/10\n",
            stderr="",
        ))
        with patch("tether.git_ops.subprocess.run", mock_run):
            # patch git_push to be a no-op
            with patch("tether.git_ops.git_push"):
                result = create_pr(repo, title="My PR", body="desc", draft=False)

        assert result.forge == "github"
        assert result.url == "https://github.com/owner/repo/pull/10"
        assert result.number == 10
        assert result.draft is False

        # Find the gh call
        calls = mock_run.call_args_list
        gh_call = next(c for c in calls if c[0][0][0] == "gh")
        cmd = gh_call[0][0]
        assert "--title" in cmd
        assert "My PR" in cmd
        assert "--body" in cmd

    def test_creates_draft_pr(self, tmp_path):
        """draft=True adds --draft to the gh command."""
        repo = _make_git_repo(str(tmp_path / "repo"))

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/pull/11\n",
            stderr="",
        ))
        with patch("tether.git_ops.subprocess.run", mock_run):
            with patch("tether.git_ops.git_push"):
                result = create_pr(repo, title="Draft", draft=True)

        assert result.draft is True
        calls = mock_run.call_args_list
        gh_call = next(c for c in calls if c[0][0][0] == "gh")
        assert "--draft" in gh_call[0][0]

    def test_creates_pr_with_base(self, tmp_path):
        """base= is forwarded as --base <branch>."""
        repo = _make_git_repo(str(tmp_path / "repo"))

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/pull/12\n",
            stderr="",
        ))
        with patch("tether.git_ops.subprocess.run", mock_run):
            with patch("tether.git_ops.git_push"):
                create_pr(repo, title="PR", base="develop")

        calls = mock_run.call_args_list
        gh_call = next(c for c in calls if c[0][0][0] == "gh")
        cmd = gh_call[0][0]
        assert "--base" in cmd
        assert "develop" in cmd

    def test_raises_when_gh_not_installed(self, tmp_path):
        """Raises ValueError with helpful message when gh is not installed."""
        repo = _make_git_repo(str(tmp_path / "repo"))

        def run_side_effect(args, **kwargs):
            if args[0] == "gh":
                raise FileNotFoundError()
            # Let all other git commands run normally
            return subprocess.run.__wrapped__(args, **kwargs) if hasattr(subprocess.run, "__wrapped__") else MagicMock(returncode=0, stdout="https://github.com/owner/repo.git\n", stderr="")

        with patch("tether.git_ops.git_push"):
            with patch("tether.git_ops._run_tool", side_effect=ValueError("'gh' is not installed or not in PATH.")):
                with pytest.raises(ValueError, match="gh.*not installed"):
                    create_pr(repo, title="PR")

    def test_raises_on_gh_failure(self, tmp_path):
        """Raises ValueError when gh exits with non-zero."""
        repo = _make_git_repo(str(tmp_path / "repo"))

        mock_run = MagicMock(return_value=MagicMock(
            returncode=1,
            stdout="",
            stderr="not authenticated",
        ))
        with patch("tether.git_ops.subprocess.run", mock_run):
            with patch("tether.git_ops.git_push"):
                with pytest.raises(ValueError, match="gh.*failed"):
                    create_pr(repo, title="PR")


class TestCreateGitlabMr:
    def test_creates_mr_for_gitlab_remote(self, tmp_path):
        """create_pr calls glab mr create for a GitLab remote."""
        import os
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-b", "main", repo], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "config", "user.email", "t@t.t"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "config", "user.name", "T"], check=True, capture_output=True)
        with open(os.path.join(repo, "f"), "w") as f:
            f.write("x")
        subprocess.run(["git", "-C", repo, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", repo, "remote", "add", "origin", "https://gitlab.com/owner/repo.git"],
            check=True, capture_output=True,
        )

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0,
            stdout="https://gitlab.com/owner/repo/-/merge_requests/5\n",
            stderr="",
        ))
        with patch("tether.git_ops.subprocess.run", mock_run):
            with patch("tether.git_ops.git_push"):
                result = create_pr(repo, title="My MR")

        assert result.forge == "gitlab"
        assert result.number == 5
        calls = mock_run.call_args_list
        glab_call = next(c for c in calls if c[0][0][0] == "glab")
        assert "mr" in glab_call[0][0]
        assert "create" in glab_call[0][0]

    def test_raises_when_glab_not_installed(self, tmp_path):
        """Raises ValueError with helpful message when glab is not installed."""
        import os
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-b", "main", repo], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "config", "user.email", "t@t.t"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "config", "user.name", "T"], check=True, capture_output=True)
        with open(os.path.join(repo, "f"), "w") as f:
            f.write("x")
        subprocess.run(["git", "-C", repo, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", repo, "remote", "add", "origin", "https://gitlab.com/owner/repo.git"],
            check=True, capture_output=True,
        )

        with patch("tether.git_ops.git_push"):
            with patch("tether.git_ops._run_tool", side_effect=ValueError("'glab' is not installed or not in PATH.")):
                with pytest.raises(ValueError, match="glab.*not installed"):
                    create_pr(repo, title="MR")


class TestCreatePrEdgeCases:
    def test_raises_without_origin_remote(self, tmp_path):
        """create_pr raises when there is no origin remote."""
        import os
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-b", "main", repo], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "config", "user.email", "t@t.t"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "config", "user.name", "T"], check=True, capture_output=True)
        with open(os.path.join(repo, "f"), "w") as f:
            f.write("x")
        subprocess.run(["git", "-C", repo, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "commit", "-m", "init"], check=True, capture_output=True)
        # no remote added

        with pytest.raises(ValueError, match="No 'origin' remote"):
            create_pr(repo, title="PR")

    def test_raises_for_unknown_forge(self, tmp_path):
        """create_pr raises for unsupported forge (e.g. Bitbucket)."""
        import os
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-b", "main", repo], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "config", "user.email", "t@t.t"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "config", "user.name", "T"], check=True, capture_output=True)
        with open(os.path.join(repo, "f"), "w") as f:
            f.write("x")
        subprocess.run(["git", "-C", repo, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", repo, "remote", "add", "origin", "https://bitbucket.org/owner/repo.git"],
            check=True, capture_output=True,
        )

        with pytest.raises(ValueError, match="Unsupported forge"):
            create_pr(repo, title="PR")

    def test_auto_push_called_by_default(self, tmp_path):
        """auto_push=True (default) calls git_push before creating PR."""
        repo = _make_git_repo(str(tmp_path / "repo"))

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/pull/1\n",
            stderr="",
        ))
        with patch("tether.git_ops.subprocess.run", mock_run):
            with patch("tether.git_ops.git_push") as mock_push:
                create_pr(repo, title="PR")

        mock_push.assert_called_once()

    def test_auto_push_skipped_when_disabled(self, tmp_path):
        """auto_push=False does not call git_push."""
        repo = _make_git_repo(str(tmp_path / "repo"))

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/pull/1\n",
            stderr="",
        ))
        with patch("tether.git_ops.subprocess.run", mock_run):
            with patch("tether.git_ops.git_push") as mock_push:
                create_pr(repo, title="PR", auto_push=False)

        mock_push.assert_not_called()


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestPrApiEndpoint:
    @pytest.mark.anyio
    async def test_create_pr_success(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore, tmp_path
    ) -> None:
        """POST /sessions/{id}/git/pr returns 201 with PR details."""
        repo = _make_git_repo(str(tmp_path / "repo"))

        session = fresh_store.create_session("test", "main")
        fresh_store.set_workdir(session.id, repo, managed=False)

        mock_result = PrResult(
            url="https://github.com/owner/repo/pull/7",
            number=7,
            forge="github",
            draft=False,
        )
        with patch("tether.api.git.create_pr", return_value=mock_result):
            resp = await api_client.post(
                f"/api/sessions/{session.id}/git/pr",
                json={"title": "My PR"},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["url"] == "https://github.com/owner/repo/pull/7"
        assert data["number"] == 7
        assert data["forge"] == "github"
        assert data["draft"] is False

    @pytest.mark.anyio
    async def test_create_pr_passes_all_fields(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore, tmp_path
    ) -> None:
        """POST /sessions/{id}/git/pr forwards title, body, base, draft."""
        repo = _make_git_repo(str(tmp_path / "repo"))
        session = fresh_store.create_session("test", "main")
        fresh_store.set_workdir(session.id, repo, managed=False)

        captured: dict = {}

        def fake_create_pr(path, title, body="", base=None, draft=False, auto_push=True):
            captured.update({"title": title, "body": body, "base": base, "draft": draft})
            return PrResult(url="https://github.com/owner/repo/pull/1", number=1, forge="github", draft=draft)

        with patch("tether.api.git.create_pr", side_effect=fake_create_pr):
            await api_client.post(
                f"/api/sessions/{session.id}/git/pr",
                json={"title": "Fix bug", "body": "Details", "base": "develop", "draft": True},
            )

        assert captured["title"] == "Fix bug"
        assert captured["body"] == "Details"
        assert captured["base"] == "develop"
        assert captured["draft"] is True

    @pytest.mark.anyio
    async def test_create_pr_session_not_found(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore
    ) -> None:
        """Returns 404 when session does not exist."""
        resp = await api_client.post(
            "/api/sessions/nonexistent/git/pr",
            json={"title": "PR"},
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_create_pr_git_error_returns_422(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore, tmp_path
    ) -> None:
        """Returns 422 when create_pr raises ValueError."""
        repo = _make_git_repo(str(tmp_path / "repo"))
        session = fresh_store.create_session("test", "main")
        fresh_store.set_workdir(session.id, repo, managed=False)

        with patch("tether.api.git.create_pr", side_effect=ValueError("gh not installed")):
            resp = await api_client.post(
                f"/api/sessions/{session.id}/git/pr",
                json={"title": "PR"},
            )

        assert resp.status_code == 422

    @pytest.mark.anyio
    async def test_create_pr_requires_title(
        self, api_client: httpx.AsyncClient, fresh_store: SessionStore, tmp_path
    ) -> None:
        """Returns 422 when title is empty."""
        repo = _make_git_repo(str(tmp_path / "repo"))
        session = fresh_store.create_session("test", "main")
        fresh_store.set_workdir(session.id, repo, managed=False)

        resp = await api_client.post(
            f"/api/sessions/{session.id}/git/pr",
            json={"title": ""},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestGitPrCli:
    def test_git_pr_parsed(self, monkeypatch):
        """tether git pr <id> -t <title> dispatches to cmd_git_pr."""
        from tether import cli_client
        from tether.cli import main
        called = {}

        def fake(sid, title, body="", base=None, draft=False, auto_push=True):
            called.update({"title": title, "draft": draft, "auto_push": auto_push})

        monkeypatch.setattr(cli_client, "cmd_git_pr", fake)
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        main(["git", "pr", "sess_abc", "-t", "Fix bug"])
        assert called["title"] == "Fix bug"
        assert called["draft"] is False
        assert called["auto_push"] is True

    def test_git_pr_draft_and_no_push(self, monkeypatch):
        """--draft and --no-push are forwarded correctly."""
        from tether import cli_client
        from tether.cli import main
        called = {}

        def fake(sid, title, body="", base=None, draft=False, auto_push=True):
            called.update({"draft": draft, "auto_push": auto_push})

        monkeypatch.setattr(cli_client, "cmd_git_pr", fake)
        monkeypatch.setattr("tether.config.load_config", lambda: None)
        main(["git", "pr", "sess_abc", "-t", "Draft MR", "--draft", "--no-push"])
        assert called["draft"] is True
        assert called["auto_push"] is False

    def test_cmd_git_pr_prints_url(self, capsys):
        """cmd_git_pr prints the PR URL after creation."""
        from tether import cli_client
        from tests.test_cli_client import _mock_response, _patch_client

        pr_resp = _mock_response(201, {
            "url": "https://github.com/owner/repo/pull/42",
            "number": 42,
            "forge": "github",
            "draft": False,
        })
        sessions_resp = _mock_response(200, [{"id": "sess_abc123", "name": "test"}])

        with _patch_client({
            ("GET", "/api/sessions"): sessions_resp,
            ("POST", "/api/sessions/sess_abc123/git/pr"): pr_resp,
        }):
            cli_client.cmd_git_pr("sess_abc123", title="Fix bug")

        out = capsys.readouterr().out
        assert "https://github.com/owner/repo/pull/42" in out
        assert "PR created" in out
