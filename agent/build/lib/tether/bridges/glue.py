"""Glue layer that wires agent-tether bridges to Tether's store and settings.

This module provides the callbacks and singletons that bridge the gap between
agent-tether (the library) and Tether (the application).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from agent_tether import BridgeCallbacks, BridgeConfig, BridgeManager
from agent_tether.subscriber import BridgeSubscriber
from tether.settings import settings

logger = structlog.get_logger(__name__)


def make_bridge_config() -> BridgeConfig:
    """Create a BridgeConfig from Tether settings."""
    return BridgeConfig(
        data_dir=settings.data_dir(),
        default_adapter=settings.adapter(),
        error_debounce_seconds=int(settings.bridge_error_debounce_seconds() or 0),
    )


def get_session_directory(session_id: str) -> str | None:
    """Callback: get session directory from the store."""
    from tether.store import store

    session = store.get_session(session_id)
    if session:
        return session.directory
    return None


def get_session_info(session_id: str) -> dict | None:
    """Callback: get session info dict from the store."""
    from tether.store import store

    session = store.get_session(session_id)
    if not session:
        return None
    return {
        "id": session.id,
        "directory": session.directory,
        "adapter": session.adapter,
        "state": session.state,
        "platform": session.platform,
        "platform_thread_id": session.platform_thread_id,
    }


async def on_session_bound(session_id: str, platform: str, thread_id: str | None) -> None:
    """Callback: bind session to platform and start subscriber."""
    from tether.store import store

    db_session = store.get_session(session_id)
    if db_session:
        db_session.platform = platform
        db_session.platform_thread_id = thread_id
        store.update_session(db_session)

    bridge_subscriber.subscribe(session_id, platform)


def get_sessions_for_restore() -> list[dict]:
    """Get all sessions as dicts for bridge thread mapping restoration."""
    from tether.store import store

    result = []
    for session in store.list_sessions():
        result.append({
            "id": session.id,
            "platform": session.platform,
            "platform_thread_id": session.platform_thread_id,
        })
    return result


# ------------------------------------------------------------------
# BridgeCallbacks implementations
# ------------------------------------------------------------------


async def _create_session(**kwargs) -> dict:
    """Create a new Tether session."""
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"http://localhost:{settings.port()}/api/sessions",
            json=kwargs,
            headers=_api_headers(),
            timeout=30.0,
        )
        response.raise_for_status()
    return response.json()


async def _send_input(session_id: str, text: str) -> None:
    """Send input to a session; start it if in CREATED state."""
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"http://localhost:{settings.port()}/api/sessions/{session_id}/input",
                json={"text": text},
                headers=_api_headers(),
                timeout=30.0,
            )
            r.raise_for_status()
        return
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 409:
            raise
        try:
            data = e.response.json()
        except Exception:
            data = {}
        code = (data.get("error") or {}).get("code")
        if code != "INVALID_STATE":
            raise

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}/start",
            json={"prompt": text},
            headers=_api_headers(),
            timeout=30.0,
        )
        r.raise_for_status()


async def _stop_session(session_id: str) -> None:
    """Interrupt/stop a session."""
    import httpx

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}/interrupt",
            headers=_api_headers(),
            timeout=10.0,
        )
        r.raise_for_status()


async def _respond_to_permission(
    session_id: str, request_id: str, allow: bool, message: str | None = None
) -> bool:
    """Respond to a permission request."""
    import httpx

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}/permission",
            json={
                "request_id": request_id,
                "allow": allow,
                "message": message or ("Approved" if allow else "User denied permission"),
            },
            headers=_api_headers(),
            timeout=10.0,
        )
        r.raise_for_status()
    return True


async def _list_sessions() -> list[dict]:
    """List all active sessions."""
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"http://localhost:{settings.port()}/api/sessions",
            headers=_api_headers(),
            timeout=10.0,
        )
        response.raise_for_status()
    return response.json()


async def _get_usage(session_id: str) -> dict:
    """Get token/cost usage for a session."""
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}/usage",
            headers=_api_headers(),
            timeout=10.0,
        )
        response.raise_for_status()
    return response.json()


async def _check_directory(path: str) -> dict:
    """Check if a directory path exists."""
    import httpx

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"http://localhost:{settings.port()}/api/directories/check",
            params={"path": path},
            headers=_api_headers(),
            timeout=10.0,
        )
        r.raise_for_status()
    return r.json()


async def _list_external_sessions(**kwargs) -> list[dict]:
    """List discoverable external sessions."""
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"http://localhost:{settings.port()}/api/external-sessions",
            headers=_api_headers(),
            params=kwargs,
            timeout=10.0,
        )
        response.raise_for_status()
    return response.json()


async def _get_external_history(
    external_id: str, runner_type: str, limit: int
) -> dict | None:
    """Get history for an external session."""
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"http://localhost:{settings.port()}/api/external-sessions/{external_id}/history",
            headers=_api_headers(),
            params={"runner_type": runner_type, "limit": limit},
            timeout=10.0,
        )
        response.raise_for_status()
    return response.json()


async def _attach_external(**kwargs) -> dict:
    """Attach to an external session."""
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"http://localhost:{settings.port()}/api/sessions/attach",
            json=kwargs,
            headers=_api_headers(),
            timeout=30.0,
        )
        response.raise_for_status()
    return response.json()


def _api_headers() -> dict[str, str]:
    """Build auth headers for internal API calls."""
    token = settings.token()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def make_bridge_callbacks() -> BridgeCallbacks:
    """Create BridgeCallbacks wired to Tether's API."""
    return BridgeCallbacks(
        create_session=_create_session,
        send_input=_send_input,
        stop_session=_stop_session,
        respond_to_permission=_respond_to_permission,
        list_sessions=_list_sessions,
        get_usage=_get_usage,
        check_directory=_check_directory,
        list_external_sessions=_list_external_sessions,
        get_external_history=_get_external_history,
        attach_external=_attach_external,
    )


def _new_subscriber(session_id: str) -> asyncio.Queue:
    """Store callback: register a new subscriber queue."""
    from tether.store import store

    return store.new_subscriber(session_id)


def _remove_subscriber(session_id: str, queue: asyncio.Queue) -> None:
    """Store callback: unregister a subscriber queue."""
    from tether.store import store

    store.remove_subscriber(session_id, queue)


# Singletons
bridge_manager = BridgeManager()
bridge_subscriber = BridgeSubscriber(
    bridge_manager=bridge_manager,
    new_subscriber=_new_subscriber,
    remove_subscriber=_remove_subscriber,
)
