"""Configuration discovery endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from tether.api.deps import require_token
from tether.api.schemas import (
    ConfigCapabilitiesResponse,
    PermissionCapabilitiesResponse,
)
from tether.runner.capabilities import all_permission_capabilities
from tether.settings import settings

router = APIRouter(tags=["config"])


@router.get("/config", response_model=ConfigCapabilitiesResponse)
async def get_config_capabilities(
    _: None = Depends(require_token),
) -> ConfigCapabilitiesResponse:
    """Return server capabilities that clients need for truthful controls."""
    capabilities = {
        name: PermissionCapabilitiesResponse.model_validate(data)
        for name, data in all_permission_capabilities().items()
    }
    return ConfigCapabilitiesResponse(
        default_adapter=settings.adapter(),
        permission_capabilities=capabilities,
    )
