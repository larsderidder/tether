"""Registry for managing multiple runner instances."""

from __future__ import annotations

import asyncio

import structlog

from tether.runner import Runner, RunnerEvents, get_runner, normalize_adapter_name
from tether.settings import settings

logger = structlog.get_logger(__name__)

RUNNER_SHUTDOWN_TIMEOUT_SECONDS = 15.0


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
        name = normalize_adapter_name(adapter_name or self._default_adapter)
        if not name:
            raise ValueError(
                "No adapter specified and no default adapter configured. "
                "Set TETHER_DEFAULT_AGENT_ADAPTER in your config, or pass an adapter explicitly."
            )

        # Return cached runner if exists
        if name in self._runners:
            logger.debug("Using cached runner", adapter=name)
            return self._runners[name]

        # Create new runner (validates adapter name and credentials)
        logger.info("Creating new runner", adapter=name)
        runner = get_runner(self._events, name=name)
        self._runners[name] = runner
        logger.info(
            "Runner created",
            adapter=name,
            runner_type=runner.runner_type,
        )
        return runner

    async def shutdown(
        self, timeout_s: float = RUNNER_SHUTDOWN_TIMEOUT_SECONDS
    ) -> None:
        """Shut down cached runners without dropping handles that fail cleanup."""
        runners = list(self._runners.items())

        async def shutdown_one(name: str, runner: Runner) -> str | None:
            shutdown = getattr(runner, "shutdown", None)
            if not callable(shutdown):
                logger.warning("Runner has no shutdown hook", adapter=name)
                return name
            try:
                await asyncio.wait_for(shutdown(), timeout=timeout_s)
            except asyncio.TimeoutError:
                logger.exception("Runner shutdown timed out", adapter=name)
                return name
            except asyncio.CancelledError:
                logger.warning("Runner shutdown cancelled", adapter=name)
                return name
            except Exception:
                logger.exception("Runner shutdown failed", adapter=name)
                return name
            return None

        failures = [
            name
            for name in await asyncio.gather(
                *(shutdown_one(name, runner) for name, runner in runners)
            )
            if name is not None
        ]
        failed_names = set(failures)
        for name, _runner in runners:
            if name not in failed_names:
                self._runners.pop(name, None)

        if failures:
            names = ", ".join(sorted(failures))
            raise RuntimeError(f"Runner shutdown failed for: {names}")

    def get_default_adapter(self) -> str | None:
        """Get the default adapter name from environment, or None if not configured."""
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
