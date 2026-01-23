"""Shared pytest fixtures for tether agent tests."""

import os
from typing import AsyncGenerator, Generator

import httpx
import pytest

# Set dev mode before importing app to avoid auth requirement
os.environ["TETHER_AGENT_DEV_MODE"] = "1"
os.environ["TETHER_AGENT_ADAPTER"] = "codex_cli"

from tether.main import app
from tether.store import SessionStore

# Disable auth by setting empty token on app state
app.state.agent_token = ""


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch) -> str:
    """Create a temporary data directory for test isolation."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setenv("TETHER_AGENT_DATA_DIR", data_dir)
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
    monkeypatch.setattr(tether.store, "store", new_store)
    monkeypatch.setattr(tether.api.state, "store", new_store)
    monkeypatch.setattr(tether.api.sessions, "store", new_store)
    monkeypatch.setattr(tether.api.runner_events, "store", new_store)
    monkeypatch.setattr(tether.api.emit, "store", new_store)
    monkeypatch.setattr(tether.api.debug, "store", new_store)
    yield new_store


@pytest.fixture
async def api_client(fresh_store) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Create an async HTTP client that uses the patched store."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def mock_codex_bin(tmp_path, monkeypatch) -> str:
    """Create a mock Codex binary that echoes output."""
    script_path = tmp_path / "mock_codex"
    script_path.write_text(
        """#!/usr/bin/env python3
import sys
import time

args = sys.argv[1:]
if args and args[0] == 'exec':
    # Filter out flag arguments
    filtered = []
    skip_next = False
    for arg in args[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith('--'):
            skip_next = True
            continue
        filtered.append(arg)

    if filtered and filtered[0] == 'resume':
        sess_id = filtered[1] if len(filtered) > 1 else 'unknown'
        prompt = ' '.join(filtered[2:]) if len(filtered) > 2 else ''
        print(f'Resumed {sess_id}', flush=True)
        print(f'OUTPUT: {prompt}', flush=True)
        sys.exit(0)

    prompt = ' '.join(filtered)
    print('Session ID: mock_session_123', flush=True)
    print(f'OUTPUT: {prompt}', flush=True)
    sys.exit(0)

print('Unknown command', flush=True)
sys.exit(1)
""",
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    monkeypatch.setenv("TETHER_AGENT_CODEX_BIN", str(script_path))
    return str(script_path)
