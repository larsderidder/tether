"""HTTP middleware and exception handlers."""

from __future__ import annotations

import time
import uuid

import structlog
from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from tether.models import ErrorDetail, ErrorResponse

logger = structlog.get_logger(__name__)


async def request_logging_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )
    start_time = time.monotonic()
    logger.info("Request started")
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.monotonic() - start_time) * 1000
        logger.exception("Request failed", duration_ms=round(duration_ms, 2))
        structlog.contextvars.clear_contextvars()
        raise
    duration_ms = (time.monotonic() - start_time) * 1000
    logger.info(
        "Request completed",
        status_code=response.status_code,
        duration_ms=round(duration_ms, 2),
    )
    structlog.contextvars.clear_contextvars()
    return response


async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    code_map = {
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "INVALID_STATE",
        422: "VALIDATION_ERROR",
        500: "INTERNAL_ERROR",
    }
    code = code_map.get(exc.status_code, "INTERNAL_ERROR")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": code,
                "message": str(exc.detail),
                "details": None,
            }
        },
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Invalid request",
                "details": exc.errors(),
            }
        },
    )


def raise_http_error(code: str, message: str, status_code: int) -> None:
    raise HTTPException(
        status_code=status_code,
        detail=ErrorResponse(
            error=ErrorDetail(code=code, message=message, details=None)
        ).model_dump(),
    )
