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
        "install_requires": "npm",
        "credentials_path": ".claude/.credentials.json",
    },
    "opencode": {
        "binary": "opencode",
        "install_command": "npm install -g opencode-ai",
        "install_requires": "npm",
        "credentials_path": None,
    },
    "pi": {
        "binary": "pi",
        "install_command": None,
        "install_requires": None,
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
    install_requires: str | None = None
    install_requires_met: bool = True


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
    install_requires = info.get("install_requires")
    install_requires_met = (
        shutil.which(install_requires) is not None
        if install_requires
        else True
    )
    return AgentInfo(
        name=name,
        binary=binary,
        installed=installed,
        version=version,
        authenticated=authenticated,
        install_command=info.get("install_command"),
        install_requires=install_requires,
        install_requires_met=install_requires_met,
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

    install_requires = info.get("install_requires")
    if install_requires and shutil.which(install_requires) is None:
        raise_http_error(
            "MISSING_RUNTIME",
            f"Installing '{name}' requires '{install_requires}', which is not installed on the server. "
            f"Install it first (e.g. sudo apt-get install -y {install_requires}).",
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


# ---------------------------------------------------------------------------
# Bridge configuration
# ---------------------------------------------------------------------------

_BRIDGE_ENV_VARS: dict[str, list[tuple[str, str, bool]]] = {
    # (env_var, prompt_label, required)
    "telegram": [
        ("TELEGRAM_BOT_TOKEN", "Telegram bot token", True),
        ("TELEGRAM_FORUM_GROUP_ID", "Telegram forum group ID", True),
    ],
    "slack": [
        ("SLACK_BOT_TOKEN", "Slack bot token (xoxb-...)", True),
        ("SLACK_APP_TOKEN", "Slack app token (xapp-...)", True),
        ("SLACK_CHANNEL_ID", "Slack channel ID", True),
    ],
    "discord": [
        ("DISCORD_BOT_TOKEN", "Discord bot token", True),
        ("DISCORD_CHANNEL_ID", "Discord channel ID", True),
    ],
}


class BridgeConfigRequest(BaseModel):
    """Request body for POST /setup/bridge/{name}."""

    env: dict[str, str]


class BridgeConfigResult(BaseModel):
    """Response for POST /setup/bridge/{name}."""

    ok: bool
    bridge: str
    message: str


@router.get("/bridge/vars/{name}")
async def get_bridge_vars(
    name: str,
    _: None = Depends(require_token),
) -> dict:
    """Return the env var names required for a bridge."""
    if name not in _BRIDGE_ENV_VARS:
        raise_http_error("NOT_FOUND", f"Unknown bridge: {name}", 404)
    return {
        "bridge": name,
        "vars": [
            {"key": key, "label": label, "required": required}
            for key, label, required in _BRIDGE_ENV_VARS[name]
        ],
    }


@router.post("/bridge/{name}", response_model=BridgeConfigResult)
async def configure_bridge(
    name: str,
    payload: BridgeConfigRequest,
    _: None = Depends(require_token),
) -> BridgeConfigResult:
    """Write bridge env vars to config.env and schedule a deferred service restart.

    The restart is deferred by 2 seconds so the HTTP response is sent back to
    the client before the process exits.
    """
    import asyncio

    if name not in _BRIDGE_ENV_VARS:
        raise_http_error("NOT_FOUND", f"Unknown bridge: {name}", 404)

    required_keys = {
        key for key, _, required in _BRIDGE_ENV_VARS[name] if required
    }
    missing = required_keys - set(payload.env.keys())
    if missing:
        raise_http_error(
            "MISSING_FIELDS",
            f"Missing required fields: {', '.join(sorted(missing))}",
            400,
        )

    config_path = Path.home() / ".config" / "tether" / "config.env"
    _update_config_env(config_path, payload.env)

    logger.info("Bridge configured", bridge=name)

    # Schedule restart after response is sent.
    asyncio.get_event_loop().call_later(2.0, _restart_service)

    return BridgeConfigResult(ok=True, bridge=name, message=f"{name} bridge configured.")


def _update_config_env(path: Path, updates: dict[str, str]) -> None:
    """Update or add key=value lines in a config.env file."""
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            updated_keys.add(key)
        else:
            new_lines.append(line)

    # Append any keys not already present.
    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _restart_service() -> bool:
    """Attempt to restart the tether systemd service. Returns True on success."""
    for cmd in (
        ["systemctl", "restart", "tether"],
        ["systemctl", "--user", "restart", "tether"],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode == 0:
                return True
        except Exception:
            continue
    return False
