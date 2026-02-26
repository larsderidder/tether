"""Git status and log endpoints for session workspaces."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Query

from tether.api.deps import require_token
from tether.api.errors import raise_http_error
from tether.git import has_git_repository
from tether.git_ops import GitCommit, GitStatus, git_log, git_status
from tether.store import store

router = APIRouter(tags=["git"])
logger = structlog.get_logger(__name__)


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
