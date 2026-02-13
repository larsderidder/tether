"""Registry for managing multiple runner instances."""

from __future__ import annotations

import structlog

from tether.runner import Runner, RunnerEvents, get_runner
from tether.settings import settings

logger = structlog.get_logger(__name__)


class RunnerRegistry:
    """Manages multiple runner instances for different adapters.

    Maintains one runner instance per adapter type, creating them
    lazily on first use. Runners are long-lived and shared across
    multiple sessions.
    """

    def __init__(self, events: RunnerEvents):
        self._events = events
        self._runners: dict[str, Runner] = {}
        self._default_adapter = settings.adapter()

    def get_runner(self, adapter_name: str | None = None) -> Runner:
        """Get or create runner for specified adapter.

        Args:
            adapter_name: Adapter to use, or None for default.

        Returns:
            Runner instance for the adapter.

        Raises:
            ValueError: If adapter name is invalid or missing credentials.
        """
        name = adapter_name or self._default_adapter

        # Return cached runner if exists
        if name in self._runners:
            logger.debug("Using cached runner", adapter=name)
            return self._runners[name]

        # Create new runner (validates adapter name and credentials)
        logger.info("Creating new runner", adapter=name)

        # Temporarily set adapter in settings for get_runner()
        import os
        old_value = os.environ.get("TETHER_AGENT_ADAPTER")
        os.environ["TETHER_AGENT_ADAPTER"] = name

        try:
            runner = get_runner(self._events)
            self._runners[name] = runner
            logger.info(
                "Runner created",
                adapter=name,
                runner_type=runner.runner_type,
            )
            return runner
        finally:
            # Restore original value
            if old_value is not None:
                os.environ["TETHER_AGENT_ADAPTER"] = old_value
            else:
                os.environ.pop("TETHER_AGENT_ADAPTER", None)

    def get_default_adapter(self) -> str:
        """Get the default adapter name from environment."""
        return self._default_adapter

    def validate_adapter(self, adapter_name: str) -> None:
        """Validate adapter name and credentials.

        Args:
            adapter_name: Adapter to validate.

        Raises:
            ValueError: If adapter is invalid or missing credentials.
        """
        # Attempt to get/create runner (will raise if invalid)
        self.get_runner(adapter_name)
