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

    async def _fail(path: str, payload: dict) -> tuple[int, str]:
        raise OSError("connection refused")

    monkeypatch.setattr(runner, "_post_once", _fail)

    with pytest.raises(
        RunnerUnavailableError, match="OpenCode is not reachable"
    ):
        await runner._post_json("/session/prompt_async", {"parts": []})


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

    async def _post(path: str, payload: dict) -> tuple[int, str]:
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
