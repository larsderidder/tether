"""Managed lifecycle for the OpenCode sidecar process.

This lets Tether auto-start and supervise the sidecar so users do not need to
manually run a separate process.
"""

from __future__ import annotations

import asyncio
import http.client
import os
import shlex
import socket
import urllib.parse
from pathlib import Path

import structlog

from tether.runner.base import RunnerUnavailableError
from tether.settings import settings

logger = structlog.get_logger(__name__)

_proc: asyncio.subprocess.Process | None = None
_stdout_task: asyncio.Task | None = None
_stderr_task: asyncio.Task | None = None
_lock = asyncio.Lock()


async def ensure_opencode_sidecar_started() -> None:
    """Start OpenCode sidecar if managed mode is enabled and it's not healthy."""
    if not settings.opencode_sidecar_managed():
        return

    async with _lock:
        if await _is_healthy():
            return

        await _stop_locked()
        await _spawn_locked()
        await _wait_until_healthy()


async def stop_managed_opencode_sidecar() -> None:
    """Stop managed OpenCode sidecar if we started one."""
    async with _lock:
        await _stop_locked()


async def _spawn_locked() -> None:
    """Spawn sidecar process under lock."""
    global _proc, _stdout_task, _stderr_task
    cmd = settings.opencode_sidecar_cmd().strip() or "opencode serve"
    parts = _build_sidecar_command(cmd)
    if not parts:
        raise RunnerUnavailableError(
            "Invalid OpenCode sidecar command. Set TETHER_OPENCODE_SIDECAR_CMD."
        )

    # Pass host/port from settings so the sidecar binds the expected address.
    url = urllib.parse.urlparse(settings.opencode_sidecar_url())
    env = os.environ.copy()
    env["TETHER_OPENCODE_SIDECAR_HOST"] = url.hostname or "127.0.0.1"
    env["TETHER_OPENCODE_SIDECAR_PORT"] = str(url.port or 8790)

    # OpenCode writes logs/state under XDG_DATA_HOME. In restricted environments
    # ~/.local/share may not be writable, so pin this to Tether's data dir.
    xdg_data_home = Path(settings.data_dir()) / "opencode_managed"
    xdg_data_home.mkdir(parents=True, exist_ok=True)
    env["XDG_DATA_HOME"] = str(xdg_data_home)

    # Run from the sidecar directory so `npm start` resolves package.json.
    cwd = _find_sidecar_dir()

    logger.info("Starting managed OpenCode sidecar", cmd=parts, cwd=cwd)
    try:
        _proc = await asyncio.create_subprocess_exec(
            *parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise RunnerUnavailableError(
            "OpenCode sidecar command not found. "
            "Set TETHER_OPENCODE_SIDECAR_CMD or install the sidecar."
        ) from exc
    except OSError as exc:
        raise RunnerUnavailableError(
            f"Failed to start OpenCode sidecar process: {exc}"
        ) from exc

    if _proc.stdout:
        _stdout_task = asyncio.create_task(_drain_pipe(_proc.stdout, "stdout"))
    if _proc.stderr:
        _stderr_task = asyncio.create_task(_drain_pipe(_proc.stderr, "stderr"))


async def _wait_until_healthy() -> None:
    """Poll sidecar health endpoint until ready or timeout."""
    timeout_s = float(settings.opencode_sidecar_startup_timeout_seconds())
    deadline = asyncio.get_running_loop().time() + max(0.1, timeout_s)

    while asyncio.get_running_loop().time() < deadline:
        if await _is_healthy():
            logger.info("Managed OpenCode sidecar is healthy")
            return
        await asyncio.sleep(0.1)

    await _stop_locked()
    raise RunnerUnavailableError(
        "OpenCode sidecar did not become healthy in time. "
        "Check TETHER_OPENCODE_SIDECAR_CMD and sidecar logs."
    )


async def _stop_locked() -> None:
    """Stop managed process and drain tasks under lock."""
    global _proc, _stdout_task, _stderr_task

    if _proc and _proc.returncode is None:
        _proc.terminate()
        try:
            await asyncio.wait_for(_proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            _proc.kill()
            await _proc.wait()

    for task in (_stdout_task, _stderr_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    _proc = None
    _stdout_task = None
    _stderr_task = None


async def _drain_pipe(stream: asyncio.StreamReader, label: str) -> None:
    """Drain sidecar output and send to logs."""
    try:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.info("OpenCode sidecar output", stream=label, line=text[:1000])
    except asyncio.CancelledError:
        return


async def _is_healthy() -> bool:
    """Check sidecar /health endpoint."""
    return await asyncio.to_thread(_check_health_sync)


def _check_health_sync() -> bool:
    url = urllib.parse.urlparse(settings.opencode_sidecar_url())
    if not url.hostname:
        return False
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=1.0)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        resp.read()
        return resp.status == 200
    except (socket.timeout, OSError):
        return False
    finally:
        conn.close()


def _find_sidecar_dir() -> str | None:
    """Locate the opencode-sdk-sidecar directory.

    Checks (in order):
    1. TETHER_OPENCODE_SIDECAR_DIR env var — explicit override.
    2. Walk up from this file looking for a parent that contains
       ``opencode-sdk-sidecar/package.json``. Robust against the file
       being moved within the repo and against pip installs.

    Returns the absolute path as a string, or None if not found.
    """
    override = os.environ.get("TETHER_OPENCODE_SIDECAR_DIR", "").strip()
    if override:
        p = Path(override)
        if p.is_dir():
            return str(p)
        logger.warning(
            "TETHER_OPENCODE_SIDECAR_DIR is set but not a directory; ignoring",
            path=override,
        )

    # Walk up the directory tree from this file.
    for ancestor in Path(__file__).parents:
        candidate = ancestor / "opencode-sdk-sidecar"
        if (candidate / "package.json").exists():
            return str(candidate)

    logger.warning(
        "opencode-sdk-sidecar directory not found; npm start will run without cwd"
    )
    return None


def _build_sidecar_command(cmd: str) -> list[str]:
    """Parse the sidecar command string into argv."""
    return shlex.split(cmd)
