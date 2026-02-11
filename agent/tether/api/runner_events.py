"""Bridge runner callbacks into SSE events."""

from __future__ import annotations

from tether.api.emit import (
    emit_error,
    emit_header,
    emit_heartbeat,
    emit_input_required,
    emit_metadata,
    emit_output,
    emit_permission_request,
    emit_permission_resolved,
    emit_state,
)
from tether.api.state import now, session_lock, transition
from tether.models import SessionState
from tether.runner import Runner, get_runner
from tether.store import store

# Import at end to avoid circular dependency
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from tether.api.runner_registry import RunnerRegistry


class ApiRunnerEvents:
    """Runner callbacks that bridge process events into SSE output."""

    async def on_output(
        self,
        session_id: str,
        stream: str,
        text: str,
        *,
        kind: str = "final",
        is_final: bool | None = None,
    ) -> None:
        """Handle output emitted by runners.

        Args:
            session_id: Internal session identifier.
            stream: Stream label (currently "combined").
            text: Output text.
            kind: Output kind ("step", "final", or "header").
            is_final: Optional explicit finality flag.
        """
        session = store.get_session(session_id)
        if not session:
            return
        if kind == "header":
            session.runner_header = text
            store.update_session(session)
            return
        session.last_activity_at = now()
        store.update_session(session)
        await emit_output(session, text, kind=kind, is_final=is_final)

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
        """Handle structured header from runners."""
        session = store.get_session(session_id)
        if not session:
            return
        # Store title as runner_header for basic display
        session.runner_header = title
        store.update_session(session)

        # Capture thread_id from runner for session attachment/resume
        if thread_id and thread_id != "unknown":
            # Only set if not already set (don't overwrite on reconnect)
            existing = store.get_runner_session_id(session_id)
            if not existing:
                store.set_runner_session_id(session_id, thread_id)

        await emit_header(
            session,
            title=title,
            model=model,
            provider=provider,
            sandbox=sandbox,
            approval=approval,
        )

    async def on_error(self, session_id: str, code: str, message: str) -> None:
        """Handle runner errors by transitioning state and emitting SSE."""
        async with session_lock(session_id):
            session = store.get_session(session_id)
            if not session:
                return
            if session.state != SessionState.ERROR:
                transition(session, SessionState.ERROR, ended_at=True)
                await emit_state(session)
            await emit_error(session, code, message)

    async def on_exit(self, session_id: str, exit_code: int | None) -> None:
        """Handle runner exit. Non-zero exits go to ERROR; clean exits are no-op.

        Note: Clean exits (exit_code 0 or None) are typically followed by
        on_awaiting_input, or the /interrupt endpoint handles the transition.
        """
        async with session_lock(session_id):
            session = store.get_session(session_id)
            if not session:
                return
            # Already in a terminal or idle state
            if session.state in (SessionState.AWAITING_INPUT, SessionState.INTERRUPTING, SessionState.ERROR):
                return
            # Non-zero exit code indicates an error
            if exit_code not in (0, None):
                transition(session, SessionState.ERROR, ended_at=True, exit_code=exit_code)
                await emit_state(session)

    async def on_awaiting_input(self, session_id: str) -> None:
        """Handle runner signaling it's waiting for user input."""
        async with session_lock(session_id):
            session = store.get_session(session_id)
            if not session:
                return
            if session.state in (SessionState.AWAITING_INPUT, SessionState.ERROR):
                return
            transition(session, SessionState.AWAITING_INPUT)
            await emit_state(session)

            recent = store.get_recent_output(session_id)
            last_output = recent[-1] if recent else None
            await emit_input_required(session, last_output)

    async def on_metadata(
        self, session_id: str, key: str, value: object, raw: str
    ) -> None:
        """Forward runner metadata to SSE."""
        session = store.get_session(session_id)
        if not session:
            return
        session.last_activity_at = now()
        store.update_session(session)
        await emit_metadata(session, key, value, raw)

    async def on_heartbeat(self, session_id: str, elapsed_s: float, done: bool) -> None:
        """Forward runner heartbeat to SSE."""
        session = store.get_session(session_id)
        if not session:
            return
        session.last_activity_at = now()
        store.update_session(session)
        await emit_heartbeat(session, elapsed_s, done)

    async def on_permission_request(
        self,
        session_id: str,
        request_id: str,
        tool_name: str,
        tool_input: dict,
        suggestions: list | None = None,
    ) -> None:
        """Emit a permission request event to the UI, or auto-approve if configured."""
        session = store.get_session(session_id)
        if not session:
            return

        # Auto-approve if session has approval_mode=0
        if session.approval_mode == 0:
            store.resolve_pending_permission(
                session_id,
                request_id,
                {"behavior": "allow"},
            )
            return

        await emit_permission_request(
            session,
            request_id=request_id,
            tool_name=tool_name,
            tool_input=tool_input,
            suggestions=suggestions,
        )

    async def on_permission_resolved(
        self,
        session_id: str,
        request_id: str,
        resolved_by: str,
        allowed: bool,
        message: str | None = None,
    ) -> None:
        """Emit a permission resolved event to dismiss UI dialogs."""
        session = store.get_session(session_id)
        if not session:
            return
        await emit_permission_resolved(
            session,
            request_id=request_id,
            resolved_by=resolved_by,
            allowed=allowed,
            message=message,
        )


# Lazy registry initialization to speed up startup
_registry: "RunnerRegistry | None" = None


def get_runner_registry() -> "RunnerRegistry":
    """Get the global runner registry, creating it if needed."""
    global _registry
    if _registry is None:
        from tether.api.runner_registry import RunnerRegistry
        _registry = RunnerRegistry(ApiRunnerEvents())
    return _registry


def get_api_runner(adapter_name: str | None = None) -> Runner:
    """Get runner for specified adapter, or default if none specified.

    Args:
        adapter_name: Optional adapter override.

    Returns:
        Runner instance for the adapter.
    """
    return get_runner_registry().get_runner(adapter_name)
