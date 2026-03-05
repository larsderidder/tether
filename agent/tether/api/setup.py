"""Setup API endpoints for agent provisioning.

Allows the CLI to check, install, push credentials for, and verify
agent CLIs on the remote server - all without needing SSH access.

Supported agents:
  - claude_code: Claude Code CLI (binary: claude)
  - opencode: OpenCode (binary: opencode)
  - pi: pi agent (binary: pi)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from tether.api.deps import require_token
from tether.api.errors import raise_http_error

router = APIRouter(prefix="/setup", tags=["setup"])
logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Known agents
# ---------------------------------------------------------------------------

_KNOWN_AGENTS: dict[str, dict] = {
    "claude_code": {
        "binary": "claude",
        "install_command": "npm install -g @anthropic-ai/claude-code",
        "credentials_path": ".claude/.credentials.json",
    },
    "opencode": {
        "binary": "opencode",
        "install_command": "npm install -g opencode-ai",
        "credentials_path": None,
    },
    "pi": {
        "binary": "pi",
        "install_command": None,
        "credentials_path": None,
    },
}

# ---------------------------------------------------------------------------
# Schema models (local to this module; not exported via schemas.py to keep
# the schemas file focused on session-related types)
# ---------------------------------------------------------------------------


class AgentInfo(BaseModel):
    """Status information for a single agent CLI."""

    name: str
    binary: str
    installed: bool
    version: str | None
    authenticated: bool
    install_command: str | None


class AgentListResponse(BaseModel):
    """Response for GET /setup/agents."""

    agents: list[AgentInfo]


class InstallResult(BaseModel):
    """Response for POST /setup/agents/{name}/install."""

    ok: bool
    agent: str
    version: str | None
    message: str


class CredentialsRequest(BaseModel):
    """Request body for POST /setup/agents/{name}/credentials.

    files maps relative paths (under the server user's home directory)
    to their text content.
    """

    files: dict[str, str]


class CredentialsResult(BaseModel):
    """Response for POST /setup/agents/{name}/credentials."""

    ok: bool
    agent: str
    files_written: list[str]


class VerifyResult(BaseModel):
    """Response for POST /setup/agents/{name}/verify."""

    ok: bool
    agent: str
    version: str | None
    authenticated: bool
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_version(binary: str) -> str | None:
    """Return the version string for a binary, or None if unavailable."""
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = (result.stdout or result.stderr or "").strip()
        if result.returncode == 0 and output:
            # Strip leading "v" if present (e.g. "v1.0.3" -> "1.0.3")
            for part in output.splitlines():
                part = part.strip().lstrip("v")
                if part:
                    return part
    except Exception:
        pass
    return None


def _is_authenticated(agent_name: str) -> bool:
    """Check if an agent has valid credentials."""
    info = _KNOWN_AGENTS.get(agent_name, {})
    creds_rel = info.get("credentials_path")
    if not creds_rel:
        # No credentials concept for this agent.
        return False
    creds_path = Path.home() / creds_rel
    return creds_path.exists() and creds_path.stat().st_size > 0


def _probe_agent(name: str) -> AgentInfo:
    """Return an AgentInfo by probing the current server environment."""
    info = _KNOWN_AGENTS[name]
    binary = info["binary"]
    installed = shutil.which(binary) is not None
    version = _get_version(binary) if installed else None
    authenticated = _is_authenticated(name) if installed else False
    return AgentInfo(
        name=name,
        binary=binary,
        installed=installed,
        version=version,
        authenticated=authenticated,
        install_command=info.get("install_command"),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(
    _: None = Depends(require_token),
) -> AgentListResponse:
    """Return installation and auth status for all known agent CLIs."""
    agents = [_probe_agent(name) for name in _KNOWN_AGENTS]
    logger.info("Setup agents list requested", count=len(agents))
    return AgentListResponse(agents=agents)


@router.post("/agents/{name}/install", response_model=InstallResult)
async def install_agent(
    name: str,
    _: None = Depends(require_token),
) -> InstallResult:
    """Install an agent CLI on the server.

    Runs the agent's install_command via subprocess. Requires the
    appropriate runtime (e.g. npm) to already be available.
    """
    if name not in _KNOWN_AGENTS:
        raise_http_error("NOT_FOUND", f"Unknown agent: {name}", 404)

    info = _KNOWN_AGENTS[name]
    install_cmd = info.get("install_command")

    if not install_cmd:
        raise_http_error(
            "NOT_SUPPORTED",
            f"Agent '{name}' does not support automatic installation.",
            400,
        )

    logger.info("Installing agent", agent=name, command=install_cmd)

    try:
        result = subprocess.run(
            install_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.error("Agent install timed out", agent=name)
        raise_http_error("TIMEOUT", f"Installation of '{name}' timed out.", 504)
    except Exception as exc:
        logger.error("Agent install failed", agent=name, error=str(exc))
        raise_http_error("INSTALL_FAILED", f"Installation failed: {exc}", 500)

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        logger.error("Agent install returned non-zero", agent=name, stderr=stderr)
        raise_http_error(
            "INSTALL_FAILED",
            f"Installation of '{name}' failed: {stderr[:300]}",
            500,
        )

    binary = info["binary"]
    version = _get_version(binary)
    logger.info("Agent installed", agent=name, version=version)

    return InstallResult(
        ok=True,
        agent=name,
        version=version,
        message=f"Installed {name}" + (f" v{version}" if version else ""),
    )


@router.post("/agents/{name}/credentials", response_model=CredentialsResult)
async def push_credentials(
    name: str,
    body: CredentialsRequest,
    _: None = Depends(require_token),
) -> CredentialsResult:
    """Write credential files for an agent under the server user's home directory.

    Each key in ``files`` is a path relative to ``$HOME``. The server creates
    any missing parent directories before writing.

    The channel is already authenticated with a bearer token, but callers
    should only invoke this over a trusted network (Tailscale, localhost, etc.)
    given the sensitivity of the data.
    """
    if name not in _KNOWN_AGENTS:
        raise_http_error("NOT_FOUND", f"Unknown agent: {name}", 404)

    home = Path.home()
    written: list[str] = []

    for rel_path, content in body.files.items():
        # Reject absolute paths and path traversal attempts.
        norm = os.path.normpath(rel_path)
        if os.path.isabs(norm) or norm.startswith(".."):
            raise_http_error(
                "INVALID_PATH",
                f"Path '{rel_path}' must be relative and within the home directory.",
                400,
            )

        target = home / norm
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        # Restrict to owner read/write only (credentials are sensitive).
        target.chmod(0o600)
        written.append(norm)
        logger.info("Credentials written", agent=name, path=str(target))

    return CredentialsResult(ok=True, agent=name, files_written=written)


@router.post("/agents/{name}/verify", response_model=VerifyResult)
async def verify_agent(
    name: str,
    _: None = Depends(require_token),
) -> VerifyResult:
    """Check that an agent CLI is installed and authenticated."""
    if name not in _KNOWN_AGENTS:
        raise_http_error("NOT_FOUND", f"Unknown agent: {name}", 404)

    info = _probe_agent(name)

    if not info.installed:
        return VerifyResult(
            ok=False,
            agent=name,
            version=None,
            authenticated=False,
            message=f"{name} is not installed.",
        )

    creds_required = _KNOWN_AGENTS[name].get("credentials_path") is not None
    if creds_required and not info.authenticated:
        return VerifyResult(
            ok=False,
            agent=name,
            version=info.version,
            authenticated=False,
            message=f"{name} is installed but credentials are missing.",
        )

    return VerifyResult(
        ok=True,
        agent=name,
        version=info.version,
        authenticated=info.authenticated,
        message=f"{name} is ready." + (f" (v{info.version})" if info.version else ""),
    )
