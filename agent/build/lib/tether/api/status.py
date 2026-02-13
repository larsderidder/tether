"""Status API endpoints for bridges and sessions."""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tether.bridges.glue import bridge_manager
from tether.models import SessionState
from tether.store import store

router = APIRouter(prefix="/status", tags=["status"])


class BridgeStatusInfo(BaseModel):
    """Status information for a messaging bridge."""

    platform: str
    status: str  # "running" | "error" | "not_configured"
    initialized_at: str | None = None
    error_message: str | None = None


class BridgeStatusResponse(BaseModel):
    """Response containing all bridge statuses."""

    bridges: list[BridgeStatusInfo]


class SessionActivityInfo(BaseModel):
    """Recent session activity information."""

    session_id: str
    name: str
    state: str
    platform: str | None
    last_activity_at: str
    message_count: int


class SessionStatsResponse(BaseModel):
    """Response containing session statistics."""

    total: int
    by_state: dict[str, int]
    by_platform: dict[str, int]
    recent_activity: list[SessionActivityInfo]


@router.get("/bridges", response_model=BridgeStatusResponse)
async def get_bridge_status() -> BridgeStatusResponse:
    """Get status of all messaging bridges.

    Returns information about which bridges are running, configured,
    or not configured.
    """
    registered = set(bridge_manager.list_bridges())
    expected = {"telegram", "slack", "discord"}

    bridges: list[BridgeStatusInfo] = []

    for platform in expected:
        if platform in registered:
            # Bridge is registered and running
            bridges.append(
                BridgeStatusInfo(
                    platform=platform,
                    status="running",
                    initialized_at=None,  # Could add timestamp tracking to manager
                    error_message=None,
                )
            )
        else:
            # Bridge is not configured
            bridges.append(
                BridgeStatusInfo(
                    platform=platform,
                    status="not_configured",
                    initialized_at=None,
                    error_message=None,
                )
            )

    return BridgeStatusResponse(bridges=bridges)


@router.get("/sessions", response_model=SessionStatsResponse)
async def get_session_stats() -> SessionStatsResponse:
    """Get aggregate session statistics.

    Returns:
        - Total session count
        - Session counts by state
        - Session counts by platform
        - Top 10 most recent sessions
    """
    sessions = store.list_sessions()

    # Aggregate by state
    state_counter = Counter(s.state.value for s in sessions)
    by_state = dict(state_counter)

    # Aggregate by platform
    platform_counter = Counter(s.platform or "none" for s in sessions)
    by_platform = dict(platform_counter)

    # Get recent activity (top 10 by last_activity_at)
    sorted_sessions = sorted(
        sessions,
        key=lambda s: s.last_activity_at or s.created_at,
        reverse=True,
    )[:10]

    recent_activity = [
        SessionActivityInfo(
            session_id=s.id,
            name=s.name or "Untitled",
            state=s.state.value,
            platform=s.platform,
            last_activity_at=s.last_activity_at or s.created_at,
            message_count=store.get_message_count(s.id),
        )
        for s in sorted_sessions
    ]

    return SessionStatsResponse(
        total=len(sessions),
        by_state=by_state,
        by_platform=by_platform,
        recent_activity=recent_activity,
    )
