"""Protocol definitions for runner adapters and event callbacks."""

from __future__ import annotations

from typing import Protocol


class RunnerEvents(Protocol):
    """Callbacks invoked by runners to report process activity and terminal state."""

    async def on_output(
        self,
        session_id: str,
        stream: str,
        text: str,
        *,
        kind: str = "final",
        is_final: bool | None = None,
    ) -> None: ...

    async def on_error(self, session_id: str, code: str, message: str) -> None: ...

    async def on_exit(self, session_id: str, exit_code: int | None) -> None: ...

    async def on_awaiting_input(self, session_id: str) -> None:
        """Signal that the agent has finished a turn and is waiting for user input."""
        ...

    async def on_metadata(self, session_id: str, key: str, value: object, raw: str) -> None: ...

    async def on_heartbeat(self, session_id: str, elapsed_s: float, done: bool) -> None: ...

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
    ) -> None: ...


class Runner(Protocol):
    """Adapter interface for agent backends (Codex CLI, SDK sidecar, etc.)."""

    runner_type: str
    """High-level agent type identifier (e.g., 'codex', 'claude')."""

    async def start(self, session_id: str, prompt: str, approval_choice: int) -> None: ...

    async def send_input(self, session_id: str, text: str) -> None: ...

    async def stop(self, session_id: str) -> int | None: ...
