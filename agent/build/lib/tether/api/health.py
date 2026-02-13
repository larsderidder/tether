"""Health endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from tether.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(ok=True, version="0.2.0", protocol=1)
