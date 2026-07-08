"""Tests for bundled agent integration helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest


def test_install_integrations_default_uses_detected_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default install only writes helpers for binaries on PATH."""
    from tether.agent_integrations import install_integrations

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        "tether.agent_integrations.shutil.which",
        lambda binary: "/bin/tool" if binary == "pi" else None,
    )

    results = install_integrations()

    assert [r.name for r in results] == ["pi"]
    pi_helper = tmp_path / ".pi" / "agent" / "extensions" / "tether-attach.ts"
    assert pi_helper.exists()
    helper_text = pi_helper.read_text(encoding="utf-8")
    assert "getSessionId()" in helper_text
    assert "flushCurrentSessionHeader" in helper_text
    assert not (tmp_path / ".claude" / "commands" / "tether.md").exists()


def test_install_integrations_writes_all_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit all installs every bundled helper file."""
    from tether.agent_integrations import install_integrations

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    results = install_integrations(["all"])

    assert {r.name for r in results} == {"pi", "claude", "codex"}
    assert (tmp_path / ".pi" / "agent" / "extensions" / "tether-attach.ts").exists()
    assert (tmp_path / ".claude" / "commands" / "tether.md").exists()
    assert (tmp_path / ".codex" / "prompts" / "tether.md").exists()
    assert {r.action for r in results} == {"installed"}


def test_install_integration_reports_conflict_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing different files are preserved unless force is enabled."""
    from tether.agent_integrations import install_integrations

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    target = tmp_path / ".codex" / "prompts" / "tether.md"
    target.parent.mkdir(parents=True)
    target.write_text("custom", encoding="utf-8")

    result = install_integrations(["codex"])[0]

    assert result.action == "conflict"
    assert target.read_text(encoding="utf-8") == "custom"


def test_install_integration_force_overwrites_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force replaces existing integration files."""
    from tether.agent_integrations import install_integrations

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    target = tmp_path / ".claude" / "commands" / "tether.md"
    target.parent.mkdir(parents=True)
    target.write_text("custom", encoding="utf-8")

    result = install_integrations(["claude"], force=True)[0]

    assert result.action == "updated"
    assert "tether attach-current" in target.read_text(encoding="utf-8")


def test_cmd_install_integrations_no_detected_agents_prints_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI default is friendly when no supported agents are installed."""
    from tether.cli_client import cmd_install_integrations

    with patch("tether.agent_integrations.shutil.which", return_value=None):
        cmd_install_integrations()

    out = capsys.readouterr().out
    assert "No supported agent CLIs found" in out
    assert "install all" in out


def test_cmd_attach_current_uses_running_session_and_auto_bridge(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """attach-current picks the running session in the directory."""
    from tether.cli_client import cmd_attach_current

    external = {
        "id": "codex-1",
        "runner_type": "codex",
        "directory": str(tmp_path),
        "last_activity": "2026-01-01T00:00:00Z",
        "is_running": True,
    }
    response = httpx.Response(
        201,
        json={
            "id": "sess_123",
            "state": "AWAITING_INPUT",
            "directory": str(tmp_path),
            "platform": "telegram",
        },
    )

    with patch("tether.cli_client._fetch_external_sessions", return_value=[external]):
        with patch(
            "tether.cli_client._get_running_platforms", return_value=["telegram"]
        ):
            with patch("tether.cli_client._post_attach", return_value=response) as post:
                cmd_attach_current(runner_type="codex", directory=str(tmp_path))

    assert post.call_args.args[0] == {
        "external_id": "codex-1",
        "runner_type": "codex",
        "directory": str(tmp_path),
        "platform": "telegram",
    }
    out = capsys.readouterr().out
    assert "sess_123" in out


def test_cmd_attach_current_renames_session(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """attach-current can carry the current agent session name."""
    from tether.cli_client import cmd_attach_current

    attach_response = httpx.Response(
        201,
        json={
            "id": "sess_named",
            "state": "AWAITING_INPUT",
            "directory": str(tmp_path),
            "platform": None,
            "name": None,
        },
    )
    rename_response = httpx.Response(
        200,
        json={
            "id": "sess_named",
            "state": "AWAITING_INPUT",
            "directory": str(tmp_path),
            "platform": None,
            "name": "My pi session",
        },
    )
    client = MagicMock()
    client.patch.return_value = rename_response
    context = MagicMock()
    context.__enter__.return_value = client

    with patch("tether.cli_client._post_attach", return_value=attach_response):
        with patch("tether.cli_client._client", return_value=context):
            cmd_attach_current(
                runner_type="pi",
                directory=str(tmp_path),
                external_id="pi-1",
                platform="none",
                name="  My   pi session  ",
            )

    client.patch.assert_called_once_with(
        "/api/sessions/sess_named/rename",
        json={"name": "My pi session"},
    )
    assert "My pi session" in capsys.readouterr().out


def test_cmd_attach_current_json_output(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """attach-current can print a compact machine-readable result."""
    from tether.cli_client import cmd_attach_current

    response = httpx.Response(
        201,
        json={
            "id": "sess_abc",
            "state": "AWAITING_INPUT",
            "directory": str(tmp_path),
            "platform": None,
        },
    )

    with patch("tether.cli_client._post_attach", return_value=response):
        cmd_attach_current(
            runner_type="pi",
            directory=str(tmp_path),
            external_id="pi-1",
            platform="none",
            as_json=True,
        )

    out = capsys.readouterr().out
    assert '"session_id": "sess_abc"' in out
    assert '"external_id": "pi-1"' in out
