"""Endpoints for validating local directory inputs."""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, Query

from tether.api.deps import require_token
from tether.api.diff import build_git_diff
from tether.api.schemas import DiffResponse, DirectoryCheckResponse
from tether.diff import parse_git_diff
from tether.git import has_git_repository, normalize_directory_path

router = APIRouter(tags=["directories"])
logger = structlog.get_logger(__name__)


@router.get("/directories/check", response_model=DirectoryCheckResponse)
async def check_directory(
    path: str = Query(..., min_length=1),
    _: None = Depends(require_token),
) -> DirectoryCheckResponse:
    """Return metadata about a local directory path."""
    logger.info("Directory check requested", path=path)
    normalized = normalize_directory_path(path)
    target = Path(normalized)
    exists = target.is_dir()
    is_git = exists and has_git_repository(normalized)
    logger.info(
        "Directory check completed",
        path=normalized,
        exists=exists,
        is_git=is_git,
    )
    return DirectoryCheckResponse(path=normalized, exists=exists, is_git=is_git)


@router.get("/directories/diff", response_model=DiffResponse)
async def get_directory_diff(
    path: str = Query(..., min_length=1),
    _: None = Depends(require_token),
) -> DiffResponse:
    """Return the git diff for the provided directory."""
    normalized = normalize_directory_path(path)
    target = Path(normalized)
    if not target.is_dir():
        logger.info("Directory diff requested for missing path", path=normalized)
        return DiffResponse(diff="", files=[])
    if not has_git_repository(normalized):
        logger.info("Directory diff requested for non-git path", path=normalized)
        return DiffResponse(diff="", files=[])
    logger.info("Directory diff requested", path=normalized)
    diff_text = build_git_diff(normalized)
    files = parse_git_diff(diff_text)
    return DiffResponse(diff=diff_text, files=files)
