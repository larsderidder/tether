"""Tests for OpenCode managed sidecar command building."""

from __future__ import annotations


def test_build_sidecar_command_parses_string() -> None:
    from tether.runner.opencode_sidecar_manager import _build_sidecar_command

    cmd = _build_sidecar_command("npm start")
    assert cmd == ["npm", "start"]


def test_build_sidecar_command_parses_complex() -> None:
    from tether.runner.opencode_sidecar_manager import _build_sidecar_command

    cmd = _build_sidecar_command("tsx src/index.ts --flag value")
    assert cmd == ["tsx", "src/index.ts", "--flag", "value"]


def test_build_sidecar_command_empty() -> None:
    from tether.runner.opencode_sidecar_manager import _build_sidecar_command

    assert _build_sidecar_command("") == []
    assert _build_sidecar_command("   ") == []
