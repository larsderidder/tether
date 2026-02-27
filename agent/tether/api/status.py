"""Status API endpoints for bridges and sessions."""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from tether.api.deps import require_token
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


# ---------------------------------------------------------------------------
# Workspace disk usage
# ---------------------------------------------------------------------------


class WorkspaceInfo(BaseModel):
    """Disk usage info for a single managed workspace."""

    session_id: str
    path: str
    size_bytes: int
    session_state: str | None = None
    last_activity_at: str | None = None
    is_orphan: bool = False


class WorkspaceUsageResponse(BaseModel):
    """Response for GET /status/workspaces."""

    workspaces: list[WorkspaceInfo]
    total_bytes: int
    orphan_count: int
    warning: str | None = None


@router.get("/workspaces", response_model=WorkspaceUsageResponse)
async def get_workspace_usage(
    stale_only: bool = Query(False, description="Return only workspaces for ended/deleted sessions"),
    _: None = Depends(require_token),
) -> WorkspaceUsageResponse:
    """List managed workspaces with disk usage.

    Returns all cloned workspace directories under the managed workspaces
    root, annotated with session state and a flag for orphaned directories
    (workspace exists but the session has been deleted).

    Use ``?stale_only=true`` to return only orphaned or ended-session
    workspaces (useful for identifying candidates for cleanup).
    """
    from tether.workspace import list_workspace_usage
    from tether.settings import settings as _settings

    sessions = {s.id: s for s in store.list_sessions()}
    raw = list_workspace_usage()

    infos: list[WorkspaceInfo] = []
    for entry in raw:
        sid = entry["session_id"]
        session = sessions.get(sid)
        is_orphan = session is None
        state = session.state.value if session else None
        last_activity = (
            session.last_activity_at or session.created_at if session else None
        )

        if stale_only:
            # Include orphans and sessions in terminal states
            terminal = {SessionState.ERROR}
            if not is_orphan and session and session.state not in terminal:
                # Skip active/created/awaiting sessions unless orphan
                if session.state in (
                    SessionState.RUNNING,
                    SessionState.INTERRUPTING,
                    SessionState.AWAITING_INPUT,
                    SessionState.CREATED,
                ):
                    continue

        infos.append(
            WorkspaceInfo(
                session_id=sid,
                path=entry["path"],
                size_bytes=entry["size_bytes"],
                session_state=state,
                last_activity_at=last_activity,
                is_orphan=is_orphan,
            )
        )

    total_bytes = sum(i.size_bytes for i in infos)
    orphan_count = sum(1 for i in infos if i.is_orphan)

    # Disk quota warning
    warning: str | None = None
    max_gb = _settings.workspace_max_disk_gb()
    if max_gb is not None:
        all_bytes = sum(e["size_bytes"] for e in raw)
        all_gb = all_bytes / (1024 ** 3)
        if all_gb > max_gb:
            warning = (
                f"Workspace disk usage ({all_gb:.1f} GB) exceeds "
                f"TETHER_WORKSPACE_MAX_DISK_GB ({max_gb:.1f} GB)"
            )

    return WorkspaceUsageResponse(
        workspaces=infos,
        total_bytes=total_bytes,
        orphan_count=orphan_count,
        warning=warning,
    )


@router.delete("/workspaces/orphans", response_model=dict)
async def cleanup_orphan_workspaces(
    _: None = Depends(require_token),
) -> dict:
    """Remove orphaned workspace directories (no matching session).

    Only removes directories that exist under the managed workspaces root
    and have no corresponding session in the store. Safe to call repeatedly.

    Returns ``{"removed": N, "errors": [...]}``
    """
    from tether.workspace import cleanup_workspace, find_orphan_workspaces

    sessions = {s.id for s in store.list_sessions()}
    orphans = find_orphan_workspaces(sessions)

    removed = 0
    errors: list[str] = []
    for orphan in orphans:
        try:
            cleanup_workspace(orphan["path"])
            removed += 1
        except Exception as exc:
            errors.append(f"{orphan['session_id']}: {exc}")

    return {"removed": removed, "errors": errors}
