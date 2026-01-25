"""Composition of API routers."""

from __future__ import annotations

from fastapi import APIRouter

from tether.api.debug import router as debug_router
from tether.api.directories import router as directories_router
from tether.api.events import router as events_router
from tether.api.external_sessions import router as external_sessions_router
from tether.api.health import router as health_router
from tether.api.sessions import router as sessions_router
from tether.api.spa import router as spa_router

api_router = APIRouter(prefix="/api")
api_router.include_router(sessions_router)
api_router.include_router(directories_router)
api_router.include_router(external_sessions_router)
api_router.include_router(debug_router)
api_router.include_router(health_router)
api_router.include_router(events_router)

root_router = APIRouter()
root_router.include_router(spa_router)
