"""Glue layer that wires agent-tether bridges to Tether's store and settings.

This module provides the callbacks and singletons that bridge the gap between
agent-tether (the library) and Tether (the application).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import structlog

from agent_tether import BridgeCallbacks, BridgeConfig, BridgeManager
from agent_tether.thread_naming import format_thread_name
from tether.bridges.subscriber import BridgeSubscriber
from tether.session_titles import is_auto_session_name
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
        "runner_type": session.runner_type,
        "state": session.state,
        "platform": session.platform,
        "platform_thread_id": session.platform_thread_id,
    }


async def on_session_bound(
    session_id: str, platform: str, thread_id: str | None
) -> None:
    """Callback: bind session to platform and start subscriber."""
    from tether.store import store

    db_session = store.get_session(session_id)
    if db_session:
        db_session.platform = platform
        db_session.platform_thread_id = thread_id
        store.update_session(db_session)

    bridge_subscriber.subscribe(session_id, platform)


def make_thread_name(
    *,
    directory: str | None = None,
    runner_type: str | None = None,
    adapter: str | None = None,
) -> str:
    """Generate a thread/topic name from directory and runner info.

    Format: ``"Runner / dirname"`` (or just ``"Dirname"`` if the runner
    is unknown). Bridges handle uniqueness (appending numbers) themselves.
    """
    return format_thread_name(
        directory=directory,
        runner_type=runner_type,
        adapter=adapter,
        max_len=64,
    )


def preferred_thread_name(session) -> str | None:
    """Return the user-facing session title when it should drive thread naming."""
    if not getattr(session, "name", None):
        return None
    if is_auto_session_name(session):
        return None
    return str(session.name)


def preferred_thread_name_for_platform(session, platform: str | None) -> str | None:
    """Only Slack and Discord reuse the session title as the thread title."""
    if platform not in {"slack", "discord"}:
        return None
    return preferred_thread_name(session)


async def sync_bound_thread_name(
    session_id: str,
    *,
    preferred_name: str | None = None,
) -> str | None:
    """Best-effort rename of a bound Slack/Discord thread to the session title."""
    from tether.store import store

    session = store.get_session(session_id)
    if not session or not session.platform or not session.platform_thread_id:
        return None

    desired_name = " ".join((preferred_name or session.name or "").split())
    if not desired_name:
        return None

    bridge = bridge_manager.get_bridge(session.platform)
    if bridge is None:
        logger.debug(
            "Skipping thread rename because bridge is unavailable",
            session_id=session_id,
            platform=session.platform,
        )
        return None

    rename_thread = getattr(bridge, "rename_thread", None)
    if rename_thread is None:
        logger.debug(
            "Skipping thread rename because platform does not support it",
            session_id=session_id,
            platform=session.platform,
        )
        return None

    try:
        await rename_thread(session_id, desired_name)
    except Exception:
        logger.exception(
            "Failed to rename bound thread",
            session_id=session_id,
            platform=session.platform,
            desired_name=desired_name,
        )
        return None
    return desired_name


def get_sessions_for_restore() -> list[dict]:
    """Get all sessions as dicts for bridge thread mapping restoration."""
    from tether.store import store

    result = []
    for session in store.list_sessions():
        result.append(
            {
                "id": session.id,
                "platform": session.platform,
                "platform_thread_id": session.platform_thread_id,
            }
        )
    return result


# ------------------------------------------------------------------
# BridgeCallbacks implementations
# ------------------------------------------------------------------


async def _create_session(**kwargs) -> dict:
    """Create a new Tether session."""

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
        status = e.response.status_code
        try:
            data = e.response.json()
        except Exception:
            data = {}
        code = (data.get("error") or {}).get("code", "")
        message = (data.get("error") or {}).get("message", "")

        if status == 409 and code == "INVALID_STATE":
            # Session not yet started; fall through to /start below.
            pass
        else:
            # Surface the server's error message as a plain RuntimeError so
            # bridge handlers can relay it to the user instead of crashing.
            raise RuntimeError(message or f"Agent request failed ({status})") from e

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}/start",
            json={"prompt": text},
            headers=_api_headers(),
            timeout=30.0,
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            try:
                data = e.response.json()
                message = (data.get("error") or {}).get("message", "")
            except Exception:
                message = ""
            raise RuntimeError(
                message or f"Agent request failed ({e.response.status_code})"
            ) from e


async def _stop_session(session_id: str) -> None:
    """Interrupt/stop a session."""

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

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}/permission",
            json={
                "request_id": request_id,
                "allow": allow,
                "message": message
                or ("Approved" if allow else "User denied permission"),
            },
            headers=_api_headers(),
            timeout=10.0,
        )
        r.raise_for_status()
    return True


async def _list_sessions() -> list[dict]:
    """List all active sessions."""

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

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"http://localhost:{settings.port()}/api/external-sessions/{external_id}/history",
            headers=_api_headers(),
            params={"runner_type": runner_type, "limit": limit},
            timeout=10.0,
        )
        response.raise_for_status()
    return response.json()


async def _sync_session(session_id: str) -> dict:
    """Pull new messages from an attached external session."""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"http://localhost:{settings.port()}/api/sessions/{session_id}/sync",
            headers=_api_headers(),
            timeout=30.0,
        )
        response.raise_for_status()
    return response.json()


async def _attach_external(**kwargs) -> dict:
    """Attach to an external session."""

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"http://localhost:{settings.port()}/api/sessions/attach",
            json=kwargs,
            headers=_api_headers(),
            timeout=30.0,
        )
        if response.status_code >= 400:
            # Extract the structured error message if available
            try:
                body = response.json()
                msg = body.get("detail", {}).get("error", {}).get("message", "")
            except Exception:
                msg = ""
            raise RuntimeError(
                msg or f"Attach failed with status {response.status_code}"
            )
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
        sync_session=_sync_session,
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
