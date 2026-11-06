"""Shared pytest fixtures for tether agent tests."""

import os
from typing import AsyncGenerator, Generator

import httpx
import pytest

# Set dev mode before importing app to avoid auth requirement
os.environ["TETHER_AGENT_DEV_MODE"] = "1"
os.environ["TETHER_DEFAULT_AGENT_ADAPTER"] = "pi_rpc"
os.environ["TETHER_BRIDGE_VERBOSITY"] = "high"

# Ensure host machine credentials do not affect test results. These settings are
# intentionally unprefixed and may exist in a developer/CI environment.
for k in (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_FORUM_GROUP_ID",
    "TELEGRAM_GROUP_ID",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_CHANNEL_ID",
    "DISCORD_BOT_TOKEN",
    "DISCORD_CHANNEL_ID",
    "DISCORD_REQUIRE_PAIRING",
    "DISCORD_PAIRING_CODE",
    "DISCORD_ALLOWED_USER_IDS",
):
    os.environ.pop(k, None)

from tether.main import app
from tether.store import SessionStore
from tether.api import runner_events, state
from tether.api.runner_events import ApiRunnerEvents
from tether.api.runner_registry import RunnerRegistry
from tether.runner.pi_rpc import PiRpcRunner
from tether.runner.base import RunnerUnavailableError


def fake_pi_binary(runner: PiRpcRunner) -> str:
    """Never start an installed Pi process from a component API test."""
    if runner._pi_binary is None:
        raise RunnerUnavailableError("Use a fake Pi executable in tests")
    return runner._pi_binary


# Disable auth by setting empty token on app state
app.state.agent_token = ""


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch) -> str:
    """Create a temporary data directory for test isolation."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setenv("TETHER_AGENT_DATA_DIR", data_dir)
    # Reset the db engine so it picks up the new data dir
    from tether.db import reset_engine, init_db

    reset_engine()
    init_db()
    return data_dir


@pytest.fixture
def fresh_store(temp_data_dir, monkeypatch) -> Generator[SessionStore, None, None]:
    """Create a fresh SessionStore instance with isolated storage.

    Also patches the global store singleton for API tests.
    """
    new_store = SessionStore()
    # Patch the global store in all modules that import it
    import tether.store
    import tether.api.state
    import tether.api.sessions
    import tether.api.runner_events
    import tether.api.emit
    import tether.api.debug
    import tether.api.events
    import tether.api.external_sessions
    import tether.api.git
    import tether.external_session_watcher
    import tether.external_sync
    import tether.api.status

    monkeypatch.setattr(app.state, "agent_token", "")
    monkeypatch.setattr(tether.store, "store", new_store)
    monkeypatch.setattr(tether.api.state, "store", new_store)
    monkeypatch.setattr(tether.api.sessions, "store", new_store)
    monkeypatch.setattr(tether.api.runner_events, "store", new_store)
    monkeypatch.setattr(tether.api.emit, "store", new_store)
    monkeypatch.setattr(tether.api.debug, "store", new_store)
    monkeypatch.setattr(tether.api.events, "store", new_store)
    monkeypatch.setattr(tether.api.external_sessions, "store", new_store)
    monkeypatch.setattr(tether.api.git, "store", new_store)
    monkeypatch.setattr(tether.external_session_watcher, "store", new_store)
    monkeypatch.setattr(tether.external_sync, "store", new_store)
    monkeypatch.setattr(tether.api.status, "store", new_store)

    # Bridge registrations are global process state; reset per test to avoid
    # cross-test leakage.
    from tether.bridges.manager import bridge_manager

    bridge_manager._bridges.clear()  # noqa: SLF001 (tests need isolation)
    yield new_store
    bridge_manager._bridges.clear()  # noqa: SLF001 (tests need isolation)


@pytest.fixture
async def api_client(
    fresh_store, tmp_path, monkeypatch
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Create an isolated Pi-first client; lifespan tests use a separate fixture."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(runner_events, "_registry", RunnerRegistry(ApiRunnerEvents()))
    state._session_locks.clear()
    monkeypatch.setattr(PiRpcRunner, "_get_pi_binary", fake_pi_binary)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    state._session_locks.clear()


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Force the AnyIO pytest plugin to run tests under asyncio.

    The agent is asyncio-native (FastAPI/uvicorn) and many tests use asyncio
    primitives directly (e.g. asyncio.create_task), which are incompatible with
    the trio backend.
    """
    return "asyncio"
