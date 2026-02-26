"""Git status, log, and action endpoints for session workspaces."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from tether.api.deps import require_token
from tether.api.errors import raise_http_error
from tether.git import has_git_repository
from tether.git_ops import (
    GitCommit,
    GitPushResult,
    GitStatus,
    PrResult,
    create_pr,
    git_checkout,
    git_commit,
    git_create_branch,
    git_log,
    git_push,
    git_status,
)
from tether.models import SessionState
from tether.store import store

router = APIRouter(tags=["git"])
logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class GitCommitRequest(BaseModel):
    """Request body for POST /sessions/{id}/git/commit."""

    message: str = Field(..., min_length=1)
    add_all: bool = True


class GitPushRequest(BaseModel):
    """Request body for POST /sessions/{id}/git/push."""

    remote: str = "origin"
    branch: str | None = None


class GitBranchRequest(BaseModel):
    """Request body for POST /sessions/{id}/git/branch."""

    name: str = Field(..., min_length=1)
    checkout: bool = True


class GitCheckoutRequest(BaseModel):
    """Request body for POST /sessions/{id}/git/checkout."""

    branch: str = Field(..., min_length=1)


@router.get("/sessions/{session_id}/git", response_model=GitStatus)
async def get_git_status(
    session_id: str,
    _: None = Depends(require_token),
) -> GitStatus:
    """Return the git status for a session's workspace.

    Raises 404 if the session does not exist.
    Raises 422 if the session has no directory or the directory has no git repo.
    """
    session = store.get_session(session_id)
    if not session:
        raise_http_error("NOT_FOUND", "Session not found", 404)

    directory = session.directory
    if not directory:
        raise_http_error(
            "VALIDATION_ERROR", "Session has no directory assigned", 422
        )

    if not has_git_repository(directory):
        raise_http_error(
            "VALIDATION_ERROR",
            f"Directory '{directory}' is not a git repository",
            422,
        )

    try:
        status = git_status(directory)
    except ValueError as exc:
        raise_http_error("GIT_ERROR", str(exc), 422)

    logger.info("Git status retrieved", session_id=session_id, branch=status.branch)
    return status


@router.get("/sessions/{session_id}/git/log", response_model=list[GitCommit])
async def get_git_log(
    session_id: str,
    count: int = Query(default=10, ge=1, le=100),
    _: None = Depends(require_token),
) -> list[GitCommit]:
    """Return recent commits from a session's workspace.

    Raises 404 if the session does not exist.
    Raises 422 if the session has no directory or the directory has no git repo.
    """
    session = store.get_session(session_id)
    if not session:
        raise_http_error("NOT_FOUND", "Session not found", 404)

    directory = session.directory
    if not directory:
        raise_http_error(
            "VALIDATION_ERROR", "Session has no directory assigned", 422
        )

    if not has_git_repository(directory):
        raise_http_error(
            "VALIDATION_ERROR",
            f"Directory '{directory}' is not a git repository",
            422,
        )

    try:
        commits = git_log(directory, count=count)
    except ValueError as exc:
        raise_http_error("GIT_ERROR", str(exc), 422)

    logger.info("Git log retrieved", session_id=session_id, count=len(commits))
    return commits


# ---------------------------------------------------------------------------
# Shared guard helper
# ---------------------------------------------------------------------------


def _require_git_workspace(session_id: str) -> str:
    """Validate the session exists, has a directory, and it is a git repo.

    Returns the directory path.  Raises HTTP errors on any failure.
    """
    session = store.get_session(session_id)
    if not session:
        raise_http_error("NOT_FOUND", "Session not found", 404)

    directory = session.directory
    if not directory:
        raise_http_error(
            "VALIDATION_ERROR", "Session has no directory assigned", 422
        )

    if not has_git_repository(directory):
        raise_http_error(
            "VALIDATION_ERROR",
            f"Directory '{directory}' is not a git repository",
            422,
        )

    return directory


def _block_if_running(session_id: str) -> None:
    """Raise 409 if the session is currently RUNNING or INTERRUPTING."""
    session = store.get_session(session_id)
    if session and session.state in (SessionState.RUNNING, SessionState.INTERRUPTING):
        raise_http_error(
            "INVALID_STATE",
            "Cannot perform git write operations while session is running",
            409,
        )


# ---------------------------------------------------------------------------
# Action endpoints
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_id}/git/commit", response_model=GitCommit, status_code=201)
async def do_git_commit(
    session_id: str,
    payload: GitCommitRequest,
    _: None = Depends(require_token),
) -> GitCommit:
    """Stage all changes and create a commit in the session's workspace.

    Raises 404 if session not found, 422 if no git repo, 409 if session is running.
    """
    _block_if_running(session_id)
    directory = _require_git_workspace(session_id)

    try:
        commit = git_commit(directory, payload.message, add_all=payload.add_all)
    except ValueError as exc:
        raise_http_error("GIT_ERROR", str(exc), 422)

    logger.info(
        "Git commit created",
        session_id=session_id,
        hash=commit.hash,
        message=commit.message,
    )
    return commit


@router.post("/sessions/{session_id}/git/push", response_model=GitPushResult)
async def do_git_push(
    session_id: str,
    payload: GitPushRequest,
    _: None = Depends(require_token),
) -> GitPushResult:
    """Push commits from the session's workspace to a remote.

    Raises 404 if session not found, 422 if no git repo or push fails,
    409 if session is running.
    """
    _block_if_running(session_id)
    directory = _require_git_workspace(session_id)

    try:
        result = git_push(directory, remote=payload.remote, branch=payload.branch)
    except ValueError as exc:
        raise_http_error("GIT_ERROR", str(exc), 422)

    logger.info(
        "Git push completed",
        session_id=session_id,
        remote=result.remote,
        branch=result.branch,
    )
    return result


@router.post("/sessions/{session_id}/git/branch", response_model=dict)
async def do_git_create_branch(
    session_id: str,
    payload: GitBranchRequest,
    _: None = Depends(require_token),
) -> dict:
    """Create a new branch in the session's workspace.

    Raises 404 if session not found, 422 if no git repo or branch exists,
    409 if session is running.
    """
    _block_if_running(session_id)
    directory = _require_git_workspace(session_id)

    try:
        branch = git_create_branch(directory, payload.name, checkout=payload.checkout)
    except ValueError as exc:
        raise_http_error("GIT_ERROR", str(exc), 422)

    logger.info(
        "Git branch created",
        session_id=session_id,
        branch=branch,
        checkout=payload.checkout,
    )
    return {"branch": branch}


@router.post("/sessions/{session_id}/git/checkout", response_model=dict)
async def do_git_checkout(
    session_id: str,
    payload: GitCheckoutRequest,
    _: None = Depends(require_token),
) -> dict:
    """Check out a branch in the session's workspace.

    Raises 404 if session not found, 422 if no git repo or branch doesn't exist,
    409 if session is running.
    """
    _block_if_running(session_id)
    directory = _require_git_workspace(session_id)

    try:
        branch = git_checkout(directory, payload.branch)
    except ValueError as exc:
        raise_http_error("GIT_ERROR", str(exc), 422)

    logger.info("Git checkout completed", session_id=session_id, branch=branch)
    return {"branch": branch}


class CreatePrRequest(BaseModel):
    """Request body for POST /sessions/{id}/git/pr."""

    title: str = Field(..., min_length=1)
    body: str = ""
    base: str | None = None
    draft: bool = False
    auto_push: bool = True


@router.post("/sessions/{session_id}/git/pr", response_model=PrResult, status_code=201)
async def do_create_pr(
    session_id: str,
    payload: CreatePrRequest,
    _: None = Depends(require_token),
) -> PrResult:
    """Create a pull request (GitHub) or merge request (GitLab) from the
    session's current working branch.

    Auto-detects the forge from the ``origin`` remote URL.  Requires ``gh``
    (GitHub) or ``glab`` (GitLab) to be installed and authenticated.

    Raises 404 if session not found, 422 if no git repo or forge unsupported,
    409 if session is running.
    """
    _block_if_running(session_id)
    directory = _require_git_workspace(session_id)

    try:
        result = create_pr(
            directory,
            title=payload.title,
            body=payload.body,
            base=payload.base,
            draft=payload.draft,
            auto_push=payload.auto_push,
        )
    except ValueError as exc:
        raise_http_error("GIT_ERROR", str(exc), 422)

    logger.info(
        "PR created",
        session_id=session_id,
        url=result.url,
        number=result.number,
        forge=result.forge,
    )
    return result
