from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BUILD_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_release_assets.py"
BUILD_SPEC = importlib.util.spec_from_file_location("tether_build_release_assets", BUILD_MODULE_PATH)
BUILD_MODULE = importlib.util.module_from_spec(BUILD_SPEC)
assert BUILD_SPEC is not None and BUILD_SPEC.loader is not None
sys.modules[BUILD_SPEC.name] = BUILD_MODULE
BUILD_SPEC.loader.exec_module(BUILD_MODULE)

VERSION_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "set_release_version.py"
VERSION_SPEC = importlib.util.spec_from_file_location("tether_set_release_version", VERSION_MODULE_PATH)
VERSION_MODULE = importlib.util.module_from_spec(VERSION_SPEC)
assert VERSION_SPEC is not None and VERSION_SPEC.loader is not None
sys.modules[VERSION_SPEC.name] = VERSION_MODULE
VERSION_SPEC.loader.exec_module(VERSION_MODULE)


def test_release_tag_to_python_version_normalizes_stable_and_prerelease() -> None:
    assert BUILD_MODULE.release_tag_to_python_version("v0.3.5-rc5") == "0.3.5rc5"
    assert BUILD_MODULE.release_tag_to_python_version("v0.3.5") == "0.3.5"


def test_validate_release_tag_matches_package_version_rejects_mismatch() -> None:
    with pytest.raises(SystemExit, match="release tag/version mismatch"):
        BUILD_MODULE.validate_release_tag_matches_package_version("v0.3.5", "0.3.4")


def test_set_project_version_rewrites_pyproject_version(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "tether-ai"\nversion = "0.3.5rc4"\n',
        encoding="utf-8",
    )

    VERSION_MODULE.set_project_version(pyproject, "0.3.5")

    assert 'version = "0.3.5"' in pyproject.read_text(encoding="utf-8")
