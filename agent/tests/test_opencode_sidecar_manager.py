"""Tests for OpenCode managed sidecar helpers."""

from __future__ import annotations

import shutil
from unittest.mock import patch

import pytest

from tether.runner.base import RunnerUnavailableError


def test_resolve_uses_custom_cmd_when_set(monkeypatch):
    from tether.runner.opencode_sidecar_manager import _resolve_sidecar_command

    monkeypatch.setattr(
        "tether.settings.settings.opencode_sidecar_cmd",
        staticmethod(lambda: "my-cmd --flag"),
    )
    assert _resolve_sidecar_command() == ["my-cmd", "--flag"]


def test_resolve_uses_bundled_mjs_when_no_custom_cmd(monkeypatch, tmp_path):
    from tether.runner.opencode_sidecar_manager import _resolve_sidecar_command

    monkeypatch.setattr(
        "tether.settings.settings.opencode_sidecar_cmd",
        staticmethod(lambda: ""),
    )

    fake_mjs = tmp_path / "opencode-sidecar.mjs"
    fake_mjs.write_text("// fake")

    with patch(
        "tether.runner.opencode_sidecar_manager.bundle_path",
        return_value=fake_mjs,
    ):
        result = _resolve_sidecar_command()

    node = shutil.which("node")
    assert result == [node, str(fake_mjs)]


def test_resolve_raises_when_no_node(monkeypatch, tmp_path):
    from tether.runner.opencode_sidecar_manager import _resolve_sidecar_command

    monkeypatch.setattr(
        "tether.settings.settings.opencode_sidecar_cmd",
        staticmethod(lambda: ""),
    )

    fake_mjs = tmp_path / "opencode-sidecar.mjs"
    fake_mjs.write_text("// fake")

    with patch(
        "tether.runner.opencode_sidecar_manager.bundle_path",
        return_value=fake_mjs,
    ), patch("tether.runner.opencode_sidecar_manager.shutil") as mock_shutil:
        mock_shutil.which.return_value = None
        with pytest.raises(RunnerUnavailableError, match="Node.js is required"):
            _resolve_sidecar_command()


def test_resolve_falls_back_to_source_tree(monkeypatch):
    from tether.runner.opencode_sidecar_manager import _resolve_sidecar_command

    monkeypatch.setattr(
        "tether.settings.settings.opencode_sidecar_cmd",
        staticmethod(lambda: ""),
    )

    # No bundle available, fall back to source tree walk.
    with patch(
        "tether.runner.opencode_sidecar_manager.bundle_path",
        side_effect=FileNotFoundError,
    ):
        result = _resolve_sidecar_command()

    # In the dev layout, opencode-sdk-sidecar/ exists in the repo.
    assert "--prefix" in result
    assert "start" in result
