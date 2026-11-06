"""Session creation must work with published bridge dependencies."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from agent_tether.base import BridgeCallbacks, BridgeConfig

from tether.bridges.session_creation import SessionCreationMixin


class RecordingBridge(SessionCreationMixin):
    """Exercise shared commands without a platform transport."""

    async def on_output(self, session_id, text, metadata=None) -> None:
        """Keep configuration notices off the network."""

    async def on_approval_request(self, session_id, request) -> None:
        """No approval requests are expected during creation."""

    async def on_status_change(self, session_id, status, metadata=None) -> None:
        """No status updates are expected during creation."""

    async def create_thread(self, session_id, session_name, **kwargs) -> dict:
        """Provide a deterministic thread identity."""
        return {"thread_id": "test-thread"}


def test_session_creation_imports_without_upstream_extended_commands(
    tmp_path: Path,
) -> None:
    """Published agent-tether does not provide the newer private helpers."""
    script = """
from agent_tether.text_command_bridge import TextCommandBridge
for name in ('_parse_new_args_extended', '_handle_new_extended'):
    if hasattr(TextCommandBridge, name):
        delattr(TextCommandBridge, name)
from tether.bridges.session_creation import SessionCreationMixin
"""
    env = dict(os.environ, TETHER_AGENT_DATA_DIR=str(tmp_path / "data"))
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def bridge() -> RecordingBridge:
    """Use real parsing and record only the external host callbacks."""
    callbacks = BridgeCallbacks(
        create_session=AsyncMock(return_value={"id": "sess_new", "adapter": "pi_rpc"}),
        send_input=AsyncMock(),
        stop_session=AsyncMock(),
        respond_to_permission=AsyncMock(),
        list_sessions=AsyncMock(return_value=[]),
        get_usage=AsyncMock(),
        check_directory=AsyncMock(
            side_effect=lambda path: {"exists": True, "path": path}
        ),
        list_external_sessions=AsyncMock(return_value=[]),
        get_external_history=AsyncMock(),
        attach_external=AsyncMock(),
    )
    return RecordingBridge(
        config=BridgeConfig(default_adapter="pi_rpc"), callbacks=callbacks
    )


@pytest.mark.parametrize(
    "args, expected",
    [
        (
            "--clone https://github.com/example/repo.git -b main --shallow --auto-branch",
            {
                "clone_url": "https://github.com/example/repo.git",
                "clone_branch": "main",
                "shallow": True,
                "auto_branch": True,
            },
        ),
        (
            '--template work -a pi -m "Fix the tests"',
            {"template": "work", "adapter": "pi_rpc", "message": "Fix the tests"},
        ),
        (
            '-a pi "/tmp/project with spaces"',
            {"adapter": "pi_rpc", "directory_raw": "/tmp/project with spaces"},
        ),
    ],
)
def test_extended_arguments_do_not_depend_on_upstream_parser(
    bridge, monkeypatch, args, expected
) -> None:
    """The Tether-owned command contract survives missing upstream methods."""
    monkeypatch.delattr(
        "agent_tether.text_command_bridge.TextCommandBridge._parse_new_args_extended",
        raising=False,
    )
    parsed = bridge._parse_new_args_extended(args)
    assert {key: parsed[key] for key in expected} == expected


@pytest.mark.parametrize(
    "args",
    [
        "--clone",
        "--template",
        "-a",
        "-b",
        "-m",
        "--unknown",
        "--clone repo --template work",
    ],
)
def test_invalid_extended_arguments_are_rejected(bridge, args: str) -> None:
    """Missing values and conflicting modes never become session requests."""
    with pytest.raises(ValueError):
        bridge._parse_new_args_extended(args)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "args, expected",
    [
        (
            "--clone https://github.com/example/repo.git -b main",
            {
                "clone_url": "https://github.com/example/repo.git",
                "clone_branch": "main",
                "shallow": False,
                "auto_branch": False,
                "adapter": "pi_rpc",
                "platform": "telegram",
            },
        ),
        (
            "--template work -a pi",
            {"template": "work", "adapter": "pi_rpc", "platform": "telegram"},
        ),
    ],
)
async def test_extended_creation_works_without_upstream_handler(
    bridge, monkeypatch, args, expected
) -> None:
    """Published dependencies still support Tether's clone and template commands."""
    monkeypatch.delattr(
        "agent_tether.text_command_bridge.TextCommandBridge._handle_new_extended",
        raising=False,
    )
    parsed = bridge._parse_new_args_extended(args)
    result = await bridge._handle_new_extended(None, parsed, platform="telegram")
    assert "Failed" not in result
    bridge._callbacks.create_session.assert_awaited_once_with(**expected)
