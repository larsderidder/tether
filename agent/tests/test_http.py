"""Unit tests for HTTP utilities."""

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from unittest.mock import MagicMock

from tether.http import (
    http_exception_handler,
    validation_exception_handler,
    raise_http_error,
)


class TestHttpExceptionHandler:
    """Test HTTP exception handler."""

    def test_handles_404(self) -> None:
        """404 maps to NOT_FOUND code."""
        request = MagicMock()
        exc = HTTPException(status_code=404, detail="Not found")

        response = http_exception_handler(request, exc)

        assert response.status_code == 404
        assert b"NOT_FOUND" in response.body

    def test_handles_409(self) -> None:
        """409 maps to INVALID_STATE code."""
        request = MagicMock()
        exc = HTTPException(status_code=409, detail="Conflict")

        response = http_exception_handler(request, exc)

        assert response.status_code == 409
        assert b"INVALID_STATE" in response.body

    def test_handles_422(self) -> None:
        """422 maps to VALIDATION_ERROR code."""
        request = MagicMock()
        exc = HTTPException(status_code=422, detail="Invalid")

        response = http_exception_handler(request, exc)

        assert response.status_code == 422
        assert b"VALIDATION_ERROR" in response.body

    def test_handles_401(self) -> None:
        """401 maps to UNAUTHORIZED code."""
        request = MagicMock()
        exc = HTTPException(status_code=401, detail="Unauthorized")

        response = http_exception_handler(request, exc)

        assert response.status_code == 401
        assert b"UNAUTHORIZED" in response.body

    def test_handles_403(self) -> None:
        """403 maps to FORBIDDEN code."""
        request = MagicMock()
        exc = HTTPException(status_code=403, detail="Forbidden")

        response = http_exception_handler(request, exc)

        assert response.status_code == 403
        assert b"FORBIDDEN" in response.body

    def test_handles_500(self) -> None:
        """500 maps to INTERNAL_ERROR code."""
        request = MagicMock()
        exc = HTTPException(status_code=500, detail="Server error")

        response = http_exception_handler(request, exc)

        assert response.status_code == 500
        assert b"INTERNAL_ERROR" in response.body

    def test_unknown_status_defaults_to_internal_error(self) -> None:
        """Unknown status codes default to INTERNAL_ERROR."""
        request = MagicMock()
        exc = HTTPException(status_code=418, detail="I'm a teapot")

        response = http_exception_handler(request, exc)

        assert response.status_code == 418
        assert b"INTERNAL_ERROR" in response.body

    def test_passes_through_error_dict(self) -> None:
        """Pre-formatted error dicts are passed through."""
        request = MagicMock()
        error_detail = {"error": {"code": "CUSTOM", "message": "Custom error"}}
        exc = HTTPException(status_code=400, detail=error_detail)

        response = http_exception_handler(request, exc)

        assert response.status_code == 400
        assert b"CUSTOM" in response.body


class TestValidationExceptionHandler:
    """Test validation exception handler."""

    def test_returns_422_with_details(self) -> None:
        """Validation errors return 422 with error details."""
        request = MagicMock()
        # Create a mock validation error
        exc = MagicMock(spec=RequestValidationError)
        exc.errors.return_value = [{"loc": ["body", "field"], "msg": "required"}]

        response = validation_exception_handler(request, exc)

        assert response.status_code == 422
        assert b"VALIDATION_ERROR" in response.body
        assert b"Invalid request" in response.body


class TestRaiseHttpError:
    """Test raise_http_error helper."""

    def test_raises_http_exception(self) -> None:
        """Raises HTTPException with formatted error."""
        with pytest.raises(HTTPException) as exc_info:
            raise_http_error("TEST_ERROR", "Test message", 400)

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error"]["code"] == "TEST_ERROR"
        assert exc_info.value.detail["error"]["message"] == "Test message"
