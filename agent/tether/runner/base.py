"""Protocol definitions for runner adapters and event callbacks."""

from __future__ import annotations

from typing import Protocol


class RunnerUnavailableError(RuntimeError):
    """Raised when a configured runner backend is not reachable."""


class RunnerEvents(Protocol):
    """Callbacks invoked by runners to report process activity."""

    async def on_output(
        self,
        session_id: str,
        stream: str,
        text: str,
        *,
        kind: str = "final",
        is_final: bool | None = None,
        bridge_segments: list[dict[str, str]] | None = None,
    ) -> None:
        """Agent produced text output."""
        ...

    async def on_error(self, session_id: str, code: str, message: str) -> None:
        """Agent encountered an error."""
        ...

    async def on_exit(self, session_id: str, exit_code: int | None) -> None:
        """Agent process exited."""
        ...

    async def on_awaiting_input(self, session_id: str) -> None:
        """Agent finished a turn and is waiting for user input."""
        ...

    async def on_metadata(
        self, session_id: str, key: str, value: object, raw: str
    ) -> None:
        """Agent reported metadata."""
        ...

    async def on_heartbeat(self, session_id: str, elapsed_s: float, done: bool) -> None:
        """Periodic heartbeat from the runner."""
        ...

    async def on_header(
        self,
        session_id: str,
        *,
        title: str,
        model: str | None = None,
        provider: str | None = None,
        sandbox: str | None = None,
        approval: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """Agent session initialized with metadata."""
        ...

    async def on_permission_request(
        self,
        session_id: str,
        request_id: str,
        tool_name: str,
        tool_input: dict,
        suggestions: list | None = None,
    ) -> None:
        """Agent requests permission to use a tool."""
        ...

    async def on_permission_resolved(
        self,
        session_id: str,
        request_id: str,
        resolved_by: str,
        allowed: bool,
        message: str | None = None,
    ) -> None:
        """A permission request was resolved."""
        ...


class Runner(Protocol):
    """Adapter interface for agent backends."""

    runner_type: str

    async def start(
        self,
        session_id: str,
        prompt: str,
        approval_choice: int,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        """Start a new agent turn."""
        ...

    async def send_input(
        self,
        session_id: str,
        text: str,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        """Send follow-up input to a running session."""
        ...

    async def stop(self, session_id: str) -> int | None:
        """Stop or interrupt the agent."""
        ...

    def update_permission_mode(self, session_id: str, approval_choice: int) -> None:
        """Update the permission mode for an active session."""
        ...


class RunnerLifecycle(Protocol):
    """Optional lifecycle hooks implemented by long-lived runner adapters."""

    async def recover_session(self, session_id: str) -> bool:
        """Recover a persisted running session after Tether restart.

        Return true only when the runner restored a functioning live owner for
        the session and can continue receiving callbacks.
        """
        ...

    async def dispose_session(self, session_id: str) -> bool:
        """Dispose resources owned by this runner for one Tether session."""
        ...

    async def shutdown(self) -> None:
        """Dispose every cached process, task, and connection owned by the runner."""
        ...
