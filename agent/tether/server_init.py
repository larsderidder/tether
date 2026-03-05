"""SSH-based bootstrap for remote Tether server setup.

Provides ``tether server init <host>`` which installs and configures
a Tether server on a fresh remote machine over plain SSH.

The implementation shells out to the local ``ssh`` binary so it
automatically honours ``~/.ssh/config`` (ProxyJump, IdentityFile, etc.)
and any running ssh-agent.  No Python SSH library dependency is needed.

Bootstrap sequence
------------------
1. Connect to the remote host and detect the OS / package manager.
2. Install prerequisites if missing: Python 3.11+, pipx, Node.js, git.
3. Install ``tether-ai`` via ``pipx install``.
4. Generate an auth token and write ``~/.config/tether/config.env``.
5. Install and start a systemd service.
6. Poll the health endpoint until it responds (or timeout).
7. Register the server locally in ``~/.config/tether/servers.yaml``.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable


# ---------------------------------------------------------------------------
# SSH helper
# ---------------------------------------------------------------------------


def ssh_run(
    host: str,
    command: str,
    *,
    user: str | None = None,
    timeout: int = 60,
    quiet: bool = False,
) -> tuple[int, str, str]:
    """Run a shell command on a remote host via ``ssh``.

    Returns ``(returncode, stdout, stderr)``.
    """
    target = f"{user}@{host}" if user else host
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        target,
        command,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if not quiet and result.returncode != 0 and result.stderr:
        # Callers decide whether to surface stderr; just return it.
        pass
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def ssh_check(
    host: str,
    *,
    user: str | None = None,
    timeout: int = 10,
) -> bool:
    """Return True if the SSH connection succeeds."""
    rc, _, _ = ssh_run(host, "true", user=user, timeout=timeout, quiet=True)
    return rc == 0


# ---------------------------------------------------------------------------
# Remote detection helpers
# ---------------------------------------------------------------------------

_DETECT_PKG_MANAGER = (
    "if command -v apt-get >/dev/null 2>&1; then echo apt; "
    "elif command -v dnf >/dev/null 2>&1; then echo dnf; "
    "elif command -v yum >/dev/null 2>&1; then echo yum; "
    "elif command -v pacman >/dev/null 2>&1; then echo pacman; "
    "elif command -v apk >/dev/null 2>&1; then echo apk; "
    "else echo unknown; fi"
)


def _remote_which(host: str, user: str | None, binary: str) -> bool:
    """Return True if *binary* is available on the remote host."""
    rc, _, _ = ssh_run(
        host, f"command -v {binary} >/dev/null 2>&1", user=user, quiet=True
    )
    return rc == 0


def _remote_python_version(host: str, user: str | None) -> str | None:
    """Return the Python version string if Python 3.11+ is installed."""
    rc, out, _ = ssh_run(
        host,
        r"python3 -c 'import sys; v=sys.version_info; print(f\"{v.major}.{v.minor}\")'",
        user=user,
        quiet=True,
    )
    if rc != 0:
        return None
    return out.strip() or None


def _meets_python_requirement(version_str: str | None) -> bool:
    """Return True if the version string represents Python 3.11 or newer."""
    if not version_str:
        return False
    try:
        major, minor = (int(x) for x in version_str.split(".")[:2])
        return (major, minor) >= (3, 11)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Installer commands per package manager
# ---------------------------------------------------------------------------

_INSTALL_CMDS: dict[str, dict[str, str]] = {
    "apt": {
        "python3": "sudo apt-get install -y python3 python3-pip python3-venv",
        "nodejs": "sudo apt-get install -y nodejs npm",
        "git": "sudo apt-get install -y git",
        "pipx": "python3 -m pip install --user pipx && python3 -m pipx ensurepath",
    },
    "dnf": {
        "python3": "sudo dnf install -y python3 python3-pip",
        "nodejs": "sudo dnf install -y nodejs npm",
        "git": "sudo dnf install -y git",
        "pipx": "python3 -m pip install --user pipx && python3 -m pipx ensurepath",
    },
    "yum": {
        "python3": "sudo yum install -y python3 python3-pip",
        "nodejs": "sudo yum install -y nodejs npm",
        "git": "sudo yum install -y git",
        "pipx": "python3 -m pip install --user pipx && python3 -m pipx ensurepath",
    },
    "pacman": {
        "python3": "sudo pacman -S --noconfirm python python-pip",
        "nodejs": "sudo pacman -S --noconfirm nodejs npm",
        "git": "sudo pacman -S --noconfirm git",
        "pipx": "python3 -m pip install --user pipx && python3 -m pipx ensurepath",
    },
    "apk": {
        "python3": "sudo apk add --no-cache python3 py3-pip",
        "nodejs": "sudo apk add --no-cache nodejs npm",
        "git": "sudo apk add --no-cache git",
        "pipx": "python3 -m pip install --user pipx && python3 -m pipx ensurepath",
    },
}


# ---------------------------------------------------------------------------
# Systemd unit template
# ---------------------------------------------------------------------------

_SYSTEMD_UNIT = """\
[Unit]
Description=Tether Agent Server
After=network.target

[Service]
Type=simple
ExecStart={tether_bin} start
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

_SYSTEMD_SERVICE_NAME = "tether"


# ---------------------------------------------------------------------------
# Init result
# ---------------------------------------------------------------------------


@dataclass
class ServerInitResult:
    """Outcome of a ``server init`` run."""

    name: str
    host: str
    port: int
    user: str | None
    token: str
    steps_completed: list[str] = field(default_factory=list)
    steps_skipped: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Main bootstrap function
# ---------------------------------------------------------------------------


def run_server_init(
    host: str,
    *,
    name: str | None = None,
    user: str | None = None,
    port: int = 8787,
    telegram_token: str | None = None,
    telegram_group_id: str | None = None,
    log: Callable[[str], None] | None = None,
    health_timeout: int = 60,
    servers_path_override: str | None = None,
) -> ServerInitResult:
    """Bootstrap a remote Tether server over SSH.

    Parameters
    ----------
    host:
        SSH host (name, IP, or Tailscale hostname).
    name:
        Local alias for this server (default: *host*).
    user:
        SSH user to connect as (default: current user from ssh config).
    port:
        Port for the Tether server to listen on (default: 8787).
    telegram_token / telegram_group_id:
        Optional Telegram bridge credentials to write to the remote config.
    log:
        Callback for progress messages.  Defaults to ``print``.
    health_timeout:
        Seconds to wait for the health endpoint to respond.
    servers_path_override:
        Override path for local ``servers.yaml`` (used in tests).
    """
    if log is None:
        log = print

    alias = name or host

    # --- Step 0: verify SSH connectivity -----------------------------------
    log(f"Connecting to {host}...")
    if not ssh_check(host, user=user):
        raise ConnectionError(
            f"Cannot connect to {host} via SSH. "
            "Check your SSH config and that the host is reachable."
        )
    log("  SSH connection OK")

    result = ServerInitResult(name=alias, host=host, port=port, user=user, token="")

    # --- Step 1: detect package manager ------------------------------------
    log("Detecting OS...")
    _, pkg_manager, _ = ssh_run(host, _DETECT_PKG_MANAGER, user=user)
    if pkg_manager == "unknown":
        log("  Warning: unknown package manager. Skipping package installs.")
    else:
        log(f"  Package manager: {pkg_manager}")
    result.steps_completed.append("detect_os")

    # --- Step 2: install prerequisites -------------------------------------
    _install_prerequisites(
        host, user=user, pkg_manager=pkg_manager, log=log, result=result
    )

    # --- Step 3: install tether-ai -----------------------------------------
    _install_tether(host, user=user, log=log, result=result)

    # --- Step 4: detect tether binary path ---------------------------------
    rc, tether_bin, _ = ssh_run(
        host,
        "python3 -m pipx run --no-deps tether --help >/dev/null 2>&1; "
        "command -v tether 2>/dev/null || echo $HOME/.local/bin/tether",
        user=user,
    )
    tether_bin = tether_bin.strip() or "$HOME/.local/bin/tether"

    # --- Step 5: generate token and write config ---------------------------
    token = secrets.token_hex(32)
    result.token = token
    _write_remote_config(
        host,
        user=user,
        port=port,
        token=token,
        telegram_token=telegram_token,
        telegram_group_id=telegram_group_id,
        log=log,
        result=result,
    )

    # --- Step 6: set up systemd service ------------------------------------
    _setup_systemd(host, user=user, tether_bin=tether_bin, log=log, result=result)

    # --- Step 7: wait for health -------------------------------------------
    _wait_for_health(
        host=host,
        user=user,
        port=port,
        timeout=health_timeout,
        log=log,
        result=result,
    )

    # --- Step 8: register locally ------------------------------------------
    _register_locally(
        alias=alias,
        host=host,
        port=port,
        token=token,
        servers_path_override=servers_path_override,
        log=log,
        result=result,
    )

    return result


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------


def _install_prerequisites(
    host: str,
    *,
    user: str | None,
    pkg_manager: str,
    log: Callable[[str], None],
    result: ServerInitResult,
) -> None:
    """Install Python, pipx, Node.js, and git if missing."""
    log("Checking prerequisites...")
    cmds = _INSTALL_CMDS.get(pkg_manager, {})

    # Python 3.11+
    py_version = _remote_python_version(host, user)
    if _meets_python_requirement(py_version):
        log(f"  Python {py_version} already installed")
        result.steps_skipped.append("install_python")
    else:
        log("  Installing Python 3.11+...")
        if "python3" in cmds:
            rc, _, stderr = ssh_run(host, cmds["python3"], user=user, timeout=300)
            if rc != 0:
                raise RuntimeError(f"Failed to install Python: {stderr}")
            result.steps_completed.append("install_python")
        else:
            log("  Warning: don't know how to install Python on this system")

    # pipx
    if _remote_which(host, user, "pipx"):
        log("  pipx already installed")
        result.steps_skipped.append("install_pipx")
    else:
        log("  Installing pipx...")
        if "pipx" in cmds:
            rc, _, stderr = ssh_run(host, cmds["pipx"], user=user, timeout=120)
            if rc != 0:
                raise RuntimeError(f"Failed to install pipx: {stderr}")
            result.steps_completed.append("install_pipx")
        else:
            log("  Warning: don't know how to install pipx on this system")

    # Node.js (needed for Claude Code)
    if _remote_which(host, user, "node"):
        log("  Node.js already installed")
        result.steps_skipped.append("install_nodejs")
    else:
        log("  Installing Node.js...")
        if "nodejs" in cmds:
            rc, _, stderr = ssh_run(host, cmds["nodejs"], user=user, timeout=300)
            if rc != 0:
                log(
                    f"  Warning: Node.js install failed ({stderr[:100]}). Claude Code may not work."
                )
            else:
                result.steps_completed.append("install_nodejs")
        else:
            log("  Warning: don't know how to install Node.js on this system")

    # git
    if _remote_which(host, user, "git"):
        log("  git already installed")
        result.steps_skipped.append("install_git")
    else:
        log("  Installing git...")
        if "git" in cmds:
            rc, _, stderr = ssh_run(host, cmds["git"], user=user, timeout=120)
            if rc != 0:
                raise RuntimeError(f"Failed to install git: {stderr}")
            result.steps_completed.append("install_git")
        else:
            log("  Warning: don't know how to install git on this system")


def _install_tether(
    host: str,
    *,
    user: str | None,
    log: Callable[[str], None],
    result: ServerInitResult,
) -> None:
    """Install or upgrade tether-ai via pipx."""
    log("Installing tether-ai...")
    # Check if already installed
    rc, _, _ = ssh_run(
        host, "pipx list 2>/dev/null | grep -q tether", user=user, quiet=True
    )
    if rc == 0:
        log("  Already installed; upgrading...")
        rc, _, stderr = ssh_run(
            host,
            "~/.local/bin/pipx upgrade tether-ai 2>&1 || pipx upgrade tether-ai 2>&1",
            user=user,
            timeout=180,
        )
        result.steps_completed.append("upgrade_tether")
    else:
        rc, _, stderr = ssh_run(
            host,
            "~/.local/bin/pipx install tether-ai 2>&1 || pipx install tether-ai 2>&1",
            user=user,
            timeout=180,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to install tether-ai: {stderr}")
        result.steps_completed.append("install_tether")
    log("  tether-ai installed")


def _write_remote_config(
    host: str,
    *,
    user: str | None,
    port: int,
    token: str,
    telegram_token: str | None,
    telegram_group_id: str | None,
    log: Callable[[str], None],
    result: ServerInitResult,
) -> None:
    """Write ~/.config/tether/config.env on the remote host."""
    log("Writing remote config...")

    lines = [
        f"TETHER_AGENT_TOKEN={token}",
        "TETHER_AGENT_HOST=0.0.0.0",
        f"TETHER_AGENT_PORT={port}",
        "TETHER_DEFAULT_AGENT_ADAPTER=claude_auto",
    ]
    if telegram_token:
        lines.append(f"TELEGRAM_BOT_TOKEN={telegram_token}")
    if telegram_group_id:
        lines.append(f"TELEGRAM_FORUM_GROUP_ID={telegram_group_id}")

    config_content = "\n".join(lines) + "\n"
    # Escape for shell heredoc
    escaped = config_content.replace("'", "'\\''")
    cmd = (
        "mkdir -p ~/.config/tether && "
        f"printf '%s' '{escaped}' > ~/.config/tether/config.env && "
        "chmod 600 ~/.config/tether/config.env"
    )
    rc, _, stderr = ssh_run(host, cmd, user=user)
    if rc != 0:
        raise RuntimeError(f"Failed to write remote config: {stderr}")
    result.steps_completed.append("write_remote_config")
    log("  Remote config written")


def _setup_systemd(
    host: str,
    *,
    user: str | None,
    tether_bin: str,
    log: Callable[[str], None],
    result: ServerInitResult,
) -> None:
    """Write, enable, and start the systemd service."""
    log("Setting up systemd service...")

    # Check if systemd is available
    rc, _, _ = ssh_run(
        host, "command -v systemctl >/dev/null 2>&1", user=user, quiet=True
    )
    if rc != 0:
        log("  Warning: systemd not available. Skipping service setup.")
        result.steps_skipped.append("systemd")
        return

    # Detect whether we need --user (no sudo) or system-level
    rc_sudo, _, _ = ssh_run(host, "sudo -n true 2>/dev/null", user=user, quiet=True)
    use_user_unit = rc_sudo != 0

    unit_content = _SYSTEMD_UNIT.format(tether_bin=tether_bin)
    escaped = unit_content.replace("'", "'\\''")

    if use_user_unit:
        log("  No sudo available; installing as user systemd service...")
        install_cmd = (
            "mkdir -p ~/.config/systemd/user && "
            f"printf '%s' '{escaped}' > ~/.config/systemd/user/{_SYSTEMD_SERVICE_NAME}.service && "
            f"systemctl --user daemon-reload && "
            f"systemctl --user enable {_SYSTEMD_SERVICE_NAME} && "
            f"systemctl --user restart {_SYSTEMD_SERVICE_NAME}"
        )
        # Also enable linger so the service stays up after logout
        linger_cmd = "loginctl enable-linger $(whoami) 2>/dev/null || true"
        ssh_run(host, linger_cmd, user=user, quiet=True)
    else:
        log("  Installing as system-level systemd service...")
        # Get the remote user's home dir to find the tether binary
        _, remote_home, _ = ssh_run(host, "echo $HOME", user=user)
        full_tether_bin = tether_bin.replace("$HOME", remote_home.strip())
        unit_content = _SYSTEMD_UNIT.format(tether_bin=full_tether_bin)
        escaped = unit_content.replace("'", "'\\''")
        install_cmd = (
            f"printf '%s' '{escaped}' | sudo tee /etc/systemd/system/{_SYSTEMD_SERVICE_NAME}.service > /dev/null && "
            f"sudo systemctl daemon-reload && "
            f"sudo systemctl enable {_SYSTEMD_SERVICE_NAME} && "
            f"sudo systemctl restart {_SYSTEMD_SERVICE_NAME}"
        )

    rc, _, stderr = ssh_run(host, install_cmd, user=user, timeout=60)
    if rc != 0:
        log(
            f"  Warning: systemd setup failed ({stderr[:120]}). Start manually with: tether start"
        )
        result.steps_skipped.append("systemd")
        return

    result.steps_completed.append("systemd")
    log("  Systemd service enabled and started")


def _wait_for_health(
    host: str,
    *,
    user: str | None,
    port: int,
    timeout: int,
    log: Callable[[str], None],
    result: ServerInitResult,
) -> None:
    """Poll the health endpoint on the remote via SSH until it responds."""
    log(f"Waiting for server health (up to {timeout}s)...")
    deadline = time.monotonic() + timeout
    interval = 2
    while time.monotonic() < deadline:
        rc, out, _ = ssh_run(
            host,
            f"curl -sf http://127.0.0.1:{port}/api/health 2>/dev/null | grep -q 'ok'",
            user=user,
            quiet=True,
        )
        if rc == 0:
            result.steps_completed.append("health_check")
            log("  Server is healthy")
            return
        time.sleep(interval)
    log("  Warning: health check timed out. The server may still be starting.")
    result.steps_skipped.append("health_check")


def _register_locally(
    *,
    alias: str,
    host: str,
    port: int,
    token: str,
    servers_path_override: str | None,
    log: Callable[[str], None],
    result: ServerInitResult,
) -> None:
    """Register the server in the local ~/.config/tether/servers.yaml."""
    from pathlib import Path

    from tether.servers import write_server

    path = Path(servers_path_override) if servers_path_override else None
    entry: dict[str, str] = {
        "host": host,
        "port": str(port),
        "token": token,
    }
    write_server(alias, entry, path=path)
    result.steps_completed.append("register_locally")
    log(f"  Registered '{alias}' in servers.yaml")
