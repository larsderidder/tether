"""Tests for the OpenCode sidecar runner."""

from __future__ import annotations

import pytest

from tether.runner.base import RunnerUnavailableError


@pytest.mark.anyio
async def test_opencode_sidecar_post_json_raises_unavailable(monkeypatch):
    """Runner should fail fast with RunnerUnavailableError when sidecar is down."""
    monkeypatch.setenv("TETHER_OPENCODE_SIDECAR_MANAGED", "0")

    from tether.runner.opencode_sdk_sidecar import OpenCodeSidecarRunner

    runner = OpenCodeSidecarRunner(events=None)  # type: ignore[arg-type]

    def _fail(path: str, payload: dict) -> tuple[int, str]:
        raise OSError("connection refused")

    monkeypatch.setattr(runner, "_post_once", _fail)

    with pytest.raises(
        RunnerUnavailableError, match="OpenCode sidecar is not reachable"
    ):
        await runner._post_json("/sessions/start", {"session_id": "sess_1"})


@pytest.mark.anyio
async def test_opencode_sidecar_post_json_starts_managed_sidecar_on_unavailable(
    monkeypatch,
):
    """Managed mode should auto-start sidecar and retry once."""
    monkeypatch.setenv("TETHER_OPENCODE_SIDECAR_MANAGED", "1")

    from tether.runner.opencode_sdk_sidecar import OpenCodeSidecarRunner
    import tether.runner.opencode_sdk_sidecar as mod

    runner = OpenCodeSidecarRunner(events=None)  # type: ignore[arg-type]
    called = {"ensure": 0, "post": 0}

    async def _ensure() -> None:
        called["ensure"] += 1

    def _post(path: str, payload: dict) -> tuple[int, str]:
        called["post"] += 1
        if called["post"] == 1:
            raise OSError("unavailable")
        return 200, "{}"

    monkeypatch.setattr(mod, "ensure_opencode_sidecar_started", _ensure)
    monkeypatch.setattr(runner, "_post_once", _post)

    await runner._post_json("/sessions/start", {"session_id": "sess_1"})

    assert called["ensure"] == 1
    assert called["post"] == 2


def test_get_runner_uses_opencode_sidecar(monkeypatch):
    """Factory should route adapter=opencode to the sidecar runner only."""
    monkeypatch.setenv("TETHER_DEFAULT_AGENT_ADAPTER", "opencode")

    import tether.runner as runner_mod

    class _DummyEvents:
        async def on_output(self, *args, **kwargs):
            pass

        async def on_header(self, *args, **kwargs):
            pass

        async def on_error(self, *args, **kwargs):
            pass

        async def on_exit(self, *args, **kwargs):
            pass

        async def on_awaiting_input(self, *args, **kwargs):
            pass

        async def on_metadata(self, *args, **kwargs):
            pass

        async def on_heartbeat(self, *args, **kwargs):
            pass

        async def on_permission_request(self, *args, **kwargs):
            pass

        async def on_permission_resolved(self, *args, **kwargs):
            pass

    runner = runner_mod.get_runner(_DummyEvents())
    assert runner.runner_type == "opencode"
    assert runner.__class__.__name__ == "OpenCodeSidecarRunner"


@pytest.mark.anyio
async def test_opencode_sidecar_handle_event_header(monkeypatch):
    """Header events should be dispatched to on_header."""
    monkeypatch.setenv("TETHER_OPENCODE_SIDECAR_MANAGED", "0")

    import asyncio

    from tether.runner.opencode_sdk_sidecar import OpenCodeSidecarRunner

    calls = []

    class FakeEvents:
        async def on_header(self, session_id, **kwargs):
            calls.append(("header", session_id, kwargs))

    runner = OpenCodeSidecarRunner(events=FakeEvents())  # type: ignore[arg-type]
    runner._loop = asyncio.get_running_loop()

    event = {
        "type": "header",
        "data": {
            "title": "OpenCode",
            "model": "claude-sonnet-4-20250514",
            "provider": "anthropic",
            "thread_id": "oc_abc123",
        },
    }
    runner._handle_event("sess_1", event)

    # run_coroutine_threadsafe needs multiple event loop iterations to execute
    await asyncio.sleep(0.05)

    assert len(calls) == 1
    assert calls[0][0] == "header"
    assert calls[0][1] == "sess_1"
    assert calls[0][2]["model"] == "claude-sonnet-4-20250514"
    assert calls[0][2]["thread_id"] == "oc_abc123"


@pytest.mark.anyio
async def test_opencode_sidecar_handle_event_output(monkeypatch):
    """Output events should be dispatched to on_output."""
    monkeypatch.setenv("TETHER_OPENCODE_SIDECAR_MANAGED", "0")

    import asyncio

    from tether.runner.opencode_sdk_sidecar import OpenCodeSidecarRunner

    calls = []

    class FakeEvents:
        async def on_output(self, session_id, stream, text, kind=None, is_final=None):
            calls.append(("output", session_id, text, kind, is_final))

    runner = OpenCodeSidecarRunner(events=FakeEvents())  # type: ignore[arg-type]
    runner._loop = asyncio.get_running_loop()

    event = {
        "type": "output",
        "data": {"text": "Hello world", "kind": "step", "final": False},
    }
    runner._handle_event("sess_1", event)

    await asyncio.sleep(0.05)

    assert len(calls) == 1
    assert calls[0] == ("output", "sess_1", "Hello world", "step", False)


@pytest.mark.anyio
async def test_opencode_sidecar_handle_event_exit_awaiting_input(monkeypatch):
    """Exit with code 0 and no stop requested should transition to awaiting_input."""
    monkeypatch.setenv("TETHER_OPENCODE_SIDECAR_MANAGED", "0")

    import asyncio

    from tether.runner.opencode_sdk_sidecar import OpenCodeSidecarRunner
    from tether.store import store

    calls = []

    class FakeEvents:
        async def on_awaiting_input(self, session_id):
            calls.append(("awaiting_input", session_id))

        async def on_exit(self, session_id, exit_code):
            calls.append(("exit", session_id, exit_code))

    runner = OpenCodeSidecarRunner(events=FakeEvents())  # type: ignore[arg-type]
    runner._loop = asyncio.get_running_loop()

    store.clear_stop_requested("sess_1")

    event = {"type": "exit", "data": {"exit_code": 0}}
    runner._handle_event("sess_1", event)

    await asyncio.sleep(0.05)

    assert len(calls) == 1
    assert calls[0] == ("awaiting_input", "sess_1")
