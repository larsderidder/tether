"""Unit tests for the runner registry."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from tether.api.runner_registry import RunnerRegistry
from tether.runner import RunnerEvents


class MockRunnerEvents(RunnerEvents):
    """Mock runner events for testing."""

    async def on_output(self, session_id: str, stream: str, text: str, **kwargs) -> None:
        pass

    async def on_header(self, session_id: str, **kwargs) -> None:
        pass

    async def on_error(self, session_id: str, code: str, message: str) -> None:
        pass

    async def on_exit(self, session_id: str, exit_code: int | None) -> None:
        pass

    async def on_awaiting_input(self, session_id: str) -> None:
        pass

    async def on_metadata(self, session_id: str, key: str, value: object, raw: str) -> None:
        pass

    async def on_heartbeat(self, session_id: str, elapsed_s: float, done: bool) -> None:
        pass

    async def on_permission_request(
        self,
        session_id: str,
        request_id: str,
        tool_name: str,
        tool_input: dict,
        suggestions: list | None = None,
    ) -> None:
        pass

    async def on_permission_resolved(
        self,
        session_id: str,
        request_id: str,
        resolved_by: str,
        allowed: bool,
        message: str | None = None,
    ) -> None:
        pass


@pytest.fixture
def mock_events():
    """Create mock runner events."""
    return MockRunnerEvents()


@pytest.fixture
def registry(mock_events):
    """Create a registry with mock events."""
    return RunnerRegistry(mock_events)


def test_registry_initialization(registry):
    """Test that registry initializes correctly."""
    assert registry._runners == {}
    assert registry._default_adapter is not None


def test_get_default_adapter(registry):
    """Test getting the default adapter."""
    default = registry.get_default_adapter()
    assert default is not None
    assert isinstance(default, str)


def test_registry_caches_runners(registry, mock_events):
    """Test that registry caches runner instances."""
    # Set a valid adapter for testing
    with patch.dict(os.environ, {"TETHER_DEFAULT_AGENT_ADAPTER": "codex_sdk_sidecar"}):
        # Mock the runner creation
        with patch("tether.api.runner_registry.get_runner") as mock_get_runner:
            mock_runner = MagicMock()
            mock_runner.runner_type = "codex"
            mock_get_runner.return_value = mock_runner

            # First call should create runner
            runner1 = registry.get_runner("codex_sdk_sidecar")
            assert mock_get_runner.call_count == 1

            # Second call should return cached runner
            runner2 = registry.get_runner("codex_sdk_sidecar")
            assert mock_get_runner.call_count == 1
            assert runner1 is runner2


def test_registry_validates_adapter(registry):
    """Test that registry validates adapter names."""
    with patch.dict(os.environ, {"TETHER_DEFAULT_AGENT_ADAPTER": "invalid_adapter"}):
        with patch("tether.api.runner_registry.get_runner") as mock_get_runner:
            mock_get_runner.side_effect = ValueError("Unknown agent adapter: invalid_adapter")

            with pytest.raises(ValueError, match="Unknown agent adapter"):
                registry.validate_adapter("invalid_adapter")


def test_registry_uses_default_adapter(registry, mock_events):
    """Test that registry uses default adapter when none specified."""
    with patch("tether.api.runner_registry.get_runner") as mock_get_runner:
        mock_runner = MagicMock()
        mock_runner.runner_type = "codex"
        mock_get_runner.return_value = mock_runner

        # Get runner without specifying adapter
        runner = registry.get_runner(None)
        assert runner is not None


def test_registry_multiple_adapters(registry, mock_events):
    """Test that registry can manage multiple runners simultaneously."""
    with patch("tether.api.runner_registry.get_runner") as mock_get_runner:
        # Create different mock runners for different adapters
        def create_runner(events):
            mock_runner = MagicMock()
            adapter = os.environ.get("TETHER_DEFAULT_AGENT_ADAPTER")
            mock_runner.runner_type = adapter
            return mock_runner

        mock_get_runner.side_effect = create_runner

        # Get runners for different adapters
        with patch.dict(os.environ, {"TETHER_DEFAULT_AGENT_ADAPTER": "codex_sdk_sidecar"}):
            runner1 = registry.get_runner("codex_sdk_sidecar")

        with patch.dict(os.environ, {"TETHER_DEFAULT_AGENT_ADAPTER": "claude_subprocess"}):
            runner2 = registry.get_runner("claude_subprocess")

        # Should have two different runners
        assert runner1 is not runner2
        assert len(registry._runners) == 2


def test_registry_restores_env_on_error(registry):
    """Test that registry restores environment on error."""
    original_value = os.environ.get("TETHER_DEFAULT_AGENT_ADAPTER")

    with patch("tether.api.runner_registry.get_runner") as mock_get_runner:
        mock_get_runner.side_effect = ValueError("Test error")

        try:
            registry.get_runner("invalid")
        except ValueError:
            pass

        # Environment should be restored
        current_value = os.environ.get("TETHER_DEFAULT_AGENT_ADAPTER")
        assert current_value == original_value
