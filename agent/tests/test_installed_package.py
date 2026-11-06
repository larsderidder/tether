"""Exercise database upgrades from a built wheel, outside the source checkout."""

import os
import subprocess
import sys
import zipfile
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_wheel_runs_packaged_database_migrations(tmp_path: Path) -> None:
    """Installed Tether must migrate databases without a checkout's alembic.ini."""
    project = Path(__file__).resolve().parents[1]
    head = ScriptDirectory.from_config(
        Config(str(project / "alembic.ini"))
    ).get_current_head()
    assert head is not None
    wheels = tmp_path / "wheels"
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(project),
            "--no-deps",
            "--wheel-dir",
            str(wheels),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheels.glob("tether_ai-*.whl"))
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    env = dict(
        os.environ,
        PYTHONPATH=str(installed),
        TETHER_AGENT_DATA_DIR=str(tmp_path / "data"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        TETHER_EXPECTED_REVISION=head,
        HOME=str(tmp_path / "home"),
        TETHER_AGENT_DEV_MODE="1",
        TETHER_OPENCODE_SIDECAR_MANAGED="0",
    )
    script = Path(__file__).parent / "helpers" / "installed_package_smoke.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
