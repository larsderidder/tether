"""Shared error helpers for API responses."""

from __future__ import annotations

from fastapi import HTTPException

from tether.models import ErrorDetail, ErrorResponse


def raise_http_error(code: str, message: str, status_code: int) -> None:
    """Raise an HTTPException with a structured error payload.

    Args:
        code: Stable error code string.
        message: Human-readable error message.
        status_code: HTTP status to return.
    """
    raise HTTPException(
        status_code=status_code,
        detail=ErrorResponse(
            error=ErrorDetail(code=code, message=message, details=None)
        ).model_dump(),
    )
