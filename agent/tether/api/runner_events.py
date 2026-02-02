"""Bridge runner callbacks into SSE events."""

from __future__ import annotations

from tether.api.emit import (
    emit_error,
    emit_header,
    emit_heartbeat,
    emit_input_required,
    emit_metadata,
    emit_output,
    emit_state,
)
from tether.api.state import now, transition
from tether.models import SessionState
from tether.runner import Runner, get_runner
from tether.store import store


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


# Lazy runner initialization to speed up startup
_runner: Runner | None = None


def get_api_runner() -> Runner:
    """Get the runner instance, initializing lazily on first call."""
    global _runner
    if _runner is None:
        _runner = get_runner(ApiRunnerEvents())
    return _runner
