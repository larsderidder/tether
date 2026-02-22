"""Tests for OpenCode managed sidecar helpers."""

from __future__ import annotations

import os
from pathlib import Path


def test_build_sidecar_command_parses_string() -> None:
    from tether.runner.opencode_sidecar_manager import _build_sidecar_command

    assert _build_sidecar_command("npm start") == ["npm", "start"]


def test_build_sidecar_command_parses_complex() -> None:
    from tether.runner.opencode_sidecar_manager import _build_sidecar_command

    assert _build_sidecar_command("tsx src/index.ts --flag value") == [
        "tsx",
        "src/index.ts",
        "--flag",
        "value",
    ]


def test_build_sidecar_command_empty() -> None:
    from tether.runner.opencode_sidecar_manager import _build_sidecar_command

    assert _build_sidecar_command("") == []
    assert _build_sidecar_command("   ") == []


def test_find_sidecar_dir_returns_path_containing_package_json() -> None:
    from tether.runner.opencode_sidecar_manager import _find_sidecar_dir

    result = _find_sidecar_dir()
    # In the development layout the sidecar dir exists alongside the agent.
    if result is not None:
        assert Path(result, "package.json").exists()


def test_find_sidecar_dir_env_override(tmp_path, monkeypatch) -> None:
    from tether.runner.opencode_sidecar_manager import _find_sidecar_dir

    # Valid directory override.
    monkeypatch.setenv("TETHER_OPENCODE_SIDECAR_DIR", str(tmp_path))
    result = _find_sidecar_dir()
    assert result == str(tmp_path)


def test_find_sidecar_dir_invalid_env_override_falls_through(monkeypatch) -> None:
    from tether.runner.opencode_sidecar_manager import _find_sidecar_dir

    # Non-existent override falls through to the walk-up logic.
    monkeypatch.setenv("TETHER_OPENCODE_SIDECAR_DIR", "/nonexistent/path/xyz")
    result = _find_sidecar_dir()
    # Either finds the real dir via walk-up or returns None — never the bad path.
    assert result != "/nonexistent/path/xyz"
