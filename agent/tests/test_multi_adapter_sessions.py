"""Integration tests for multi-adapter session support."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tether.main import app
from tether.api.runner_registry import RunnerRegistry
from tether.models import SessionState

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def setup_test_env():
    """Set up test environment variables."""
    with patch.dict(
        os.environ,
        {
            "TETHER_AGENT_TOKEN": "test-token",
            "TETHER_DEFAULT_AGENT_ADAPTER": "codex_sdk_sidecar",
        },
    ):
        yield


@pytest.fixture
def mock_runner():
    """Create a mock runner."""
    runner = MagicMock()
    runner.runner_type = "codex"
    runner.start = AsyncMock()
    runner.stop = AsyncMock()
    runner.send_input = AsyncMock()
    runner.update_permission_mode = MagicMock()
    return runner


@pytest.fixture
def mock_claude_runner():
    """Create a mock Claude runner."""
    runner = MagicMock()
    runner.runner_type = "claude-subprocess"
    runner.start = AsyncMock()
    runner.stop = AsyncMock()
    runner.send_input = AsyncMock()
    runner.update_permission_mode = MagicMock()
    return runner


async def test_create_session_with_adapter(api_client, tmpdir):
    """Test creating a session with specific adapter."""
    test_dir = str(tmpdir.mkdir("test_project"))

    with patch("tether.api.runner_registry.get_runner") as mock_get_runner:
        mock_runner = MagicMock()
        mock_runner.runner_type = "claude-subprocess"
        mock_get_runner.return_value = mock_runner

        response = await api_client.post(
            "/api/sessions",
            json={"directory": test_dir, "adapter": "claude_subprocess"},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["adapter"] == "claude_subprocess"
        assert data["directory"] == test_dir


async def test_create_session_without_adapter(api_client, tmpdir):
    """Test creating a session without adapter uses default."""
    test_dir = str(tmpdir.mkdir("test_project"))

    response = await api_client.post(
        "/api/sessions",
        json={"directory": test_dir},
    )

    assert response.status_code == 201
    data = response.json()
    # Should use default adapter (None means default)
    assert data["adapter"] is None


async def test_create_session_invalid_adapter(api_client, tmpdir):
    """Test creating a session with invalid adapter returns error."""
    test_dir = str(tmpdir.mkdir("test_project"))

    with patch("tether.api.runner_registry.get_runner") as mock_get_runner:
        mock_get_runner.side_effect = ValueError("Unknown agent adapter: invalid_adapter")

        response = await api_client.post(
            "/api/sessions",
            json={"directory": test_dir, "adapter": "invalid_adapter"},
        )

        assert response.status_code == 422
        data = response.json()
        assert "Invalid adapter" in data["error"]["message"]
        assert "invalid_adapter" in data["error"]["message"]


async def test_session_adapter_routing_on_start(
    api_client, tmpdir, mock_runner, mock_claude_runner
):
    """Test that sessions route to correct runner on start."""
    test_dir1 = str(tmpdir.mkdir("test_project1"))
    test_dir2 = str(tmpdir.mkdir("test_project2"))

    with patch("tether.api.runner_events.get_runner_registry") as mock_get_registry:
        mock_registry = MagicMock(spec=RunnerRegistry)

        # Set up registry to return different runners
        def get_runner_side_effect(adapter_name):
            if adapter_name == "claude_subprocess":
                return mock_claude_runner
            return mock_runner

        mock_registry.get_runner.side_effect = get_runner_side_effect
        mock_registry.validate_adapter = MagicMock()
        mock_get_registry.return_value = mock_registry

        # Create two sessions with different adapters
        response1 = await api_client.post(
            "/api/sessions",
            json={"directory": test_dir1},
        )
        session1_id = response1.json()["id"]

        response2 = await api_client.post(
            "/api/sessions",
            json={"directory": test_dir2, "adapter": "claude_subprocess"},
        )
        session2_id = response2.json()["id"]

        # Start both sessions
        response1_start = await api_client.post(
            f"/api/sessions/{session1_id}/start",
            json={"prompt": "test prompt 1", "approval_choice": 2},
        )
        assert response1_start.status_code == 200

        response2_start = await api_client.post(
            f"/api/sessions/{session2_id}/start",
            json={"prompt": "test prompt 2", "approval_choice": 2},
        )
        assert response2_start.status_code == 200

        # Verify correct runners were used
        assert mock_runner.start.called
        assert mock_claude_runner.start.called


async def test_session_adapter_routing_on_input(
    api_client, fresh_store, tmpdir, mock_runner
):
    """Test that send_input routes to correct runner."""
    test_dir = str(tmpdir.mkdir("test_project"))

    with patch("tether.api.runner_events.get_runner_registry") as mock_get_registry:
        mock_registry = MagicMock(spec=RunnerRegistry)
        mock_registry.get_runner.return_value = mock_runner
        mock_registry.validate_adapter = MagicMock()
        mock_get_registry.return_value = mock_registry

        # Create and start session
        response = await api_client.post(
            "/api/sessions",
            json={"directory": test_dir, "adapter": "codex_sdk_sidecar"},
        )
        session_id = response.json()["id"]

        # Manually set session to AWAITING_INPUT state
        session = fresh_store.get_session(session_id)
        session.state = SessionState.AWAITING_INPUT
        fresh_store.update_session(session)

        # Send input
        input_response = await api_client.post(
            f"/api/sessions/{session_id}/input",
            json={"text": "test input"},
        )

        assert input_response.status_code == 200
        assert mock_runner.send_input.called


async def test_session_adapter_routing_on_interrupt(
    api_client, fresh_store, tmpdir, mock_runner
):
    """Test that interrupt routes to correct runner."""
    test_dir = str(tmpdir.mkdir("test_project"))

    with patch("tether.api.runner_events.get_runner_registry") as mock_get_registry:
        mock_registry = MagicMock(spec=RunnerRegistry)
        mock_registry.get_runner.return_value = mock_runner
        mock_registry.validate_adapter = MagicMock()
        mock_get_registry.return_value = mock_registry

        # Create session
        response = await api_client.post(
            "/api/sessions",
            json={"directory": test_dir, "adapter": "codex_sdk_sidecar"},
        )
        session_id = response.json()["id"]

        # Manually set session to RUNNING state
        session = fresh_store.get_session(session_id)
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        # Interrupt
        interrupt_response = await api_client.post(
            f"/api/sessions/{session_id}/interrupt",
        )

        assert interrupt_response.status_code == 200
        assert mock_runner.stop.called


async def test_session_adapter_routing_on_approval_mode(
    api_client, fresh_store, tmpdir, mock_runner
):
    """Test that approval mode update routes to correct runner."""
    test_dir = str(tmpdir.mkdir("test_project"))

    with patch("tether.api.runner_events.get_runner_registry") as mock_get_registry:
        mock_registry = MagicMock(spec=RunnerRegistry)
        mock_registry.get_runner.return_value = mock_runner
        mock_registry.validate_adapter = MagicMock()
        mock_get_registry.return_value = mock_registry

        # Create session
        response = await api_client.post(
            "/api/sessions",
            json={"directory": test_dir, "adapter": "codex_sdk_sidecar"},
        )
        session_id = response.json()["id"]

        # Manually set session to RUNNING state
        session = fresh_store.get_session(session_id)
        session.state = SessionState.RUNNING
        fresh_store.update_session(session)

        # Update approval mode
        approval_response = await api_client.patch(
            f"/api/sessions/{session_id}/approval-mode",
            json={"approval_mode": 1},
        )

        assert approval_response.status_code == 200
        assert mock_runner.update_permission_mode.called


async def test_backward_compatibility_null_adapter(
    api_client, tmpdir, mock_runner
):
    """Test that NULL adapter field uses default runner."""
    test_dir = str(tmpdir.mkdir("test_project"))

    with patch("tether.api.runner_events.get_runner_registry") as mock_get_registry:
        mock_registry = MagicMock(spec=RunnerRegistry)
        mock_registry.get_runner.return_value = mock_runner
        mock_get_registry.return_value = mock_registry

        # Create session without adapter (NULL)
        response = await api_client.post(
            "/api/sessions",
            json={"directory": test_dir},
        )
        session_id = response.json()["id"]

        # Start session - should use default runner
        start_response = await api_client.post(
            f"/api/sessions/{session_id}/start",
            json={"prompt": "test", "approval_choice": 2},
        )

        assert start_response.status_code == 200
        # Should have called get_runner with None (default)
        mock_registry.get_runner.assert_called_with(None)
