"""Transport configuration must match Tether's HTTP sidecar protocol."""

import asyncio
from unittest.mock import MagicMock

import pytest

from tether.runner.codex_sdk_sidecar import SidecarRunner


@pytest.mark.parametrize(
    "url", ["ws://127.0.0.1:8788", "wss://localhost:8788", "file:///tmp/codex"]
)
def test_sidecar_rejects_non_http_protocols(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """A native Codex app-server WebSocket is not a Tether REST sidecar."""
    monkeypatch.setenv("TETHER_CODEX_SIDECAR_URL", url)
    with pytest.raises(ValueError, match="HTTP.*sidecar"):
        SidecarRunner(MagicMock())


@pytest.mark.anyio
async def test_sidecar_posts_use_tls_and_preserve_base_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTPS reverse-proxy prefix must be used for sidecar requests."""
    monkeypatch.setenv("TETHER_CODEX_SIDECAR_URL", "https://localhost:9443/tether")
    connection = MagicMock()
    connection.getresponse.return_value.status = 200
    connection.getresponse.return_value.read.return_value = b"{}"
    factory = MagicMock(return_value=connection)
    monkeypatch.setattr(
        "tether.runner.codex_sdk_sidecar.http.client.HTTPSConnection", factory
    )
    monkeypatch.setattr(
        "tether.runner.codex_sdk_sidecar.http.client.HTTPConnection",
        MagicMock(return_value=connection),
    )
    runner = SidecarRunner(MagicMock())
    await runner._post_json("/sessions/start", {"prompt": "test"})
    factory.assert_called_once_with("localhost", 9443, timeout=10)
    assert connection.request.call_args.args[:2] == ("POST", "/tether/sessions/start")
    connection.close.assert_called_once()


@pytest.mark.anyio
async def test_blocking_sidecar_post_does_not_run_on_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow local sidecar cannot freeze every other session's control requests."""
    monkeypatch.setenv("TETHER_CODEX_SIDECAR_URL", "http://localhost:8788")
    runner = SidecarRunner(MagicMock())
    connection = MagicMock()
    connection.getresponse.return_value.status = 200
    connection.getresponse.return_value.read.return_value = b"{}"

    def request(*args, **kwargs) -> None:
        """Check the execution context inside the blocking transport call."""
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()

    connection.request.side_effect = request
    monkeypatch.setattr(
        "tether.runner.codex_sdk_sidecar.http.client.HTTPConnection",
        lambda *args, **kwargs: connection,
    )
    await runner._post_json("/sessions/start", {"prompt": "test"})
