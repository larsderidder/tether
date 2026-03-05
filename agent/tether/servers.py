"""Named remote server profiles and active context for the Tether CLI.

Reads ``~/.config/tether/servers.yaml`` (or the path in
``TETHER_SERVERS_FILE``) and exposes individual server entries so the CLI
can pick a named profile with ``--server <name>``.

The active context (stored in ``~/.config/tether/context``) provides
persistent context switching similar to kubectx.  When set, the CLI
uses that server profile without requiring ``--server`` on every command.

Example servers.yaml::

    servers:
      work:
        host: my-server.local
        port: 8787
        token: secret123
    default: work

If ``default`` is set and no active context is configured, that profile
is used as a fallback.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def default_servers_path() -> Path:
    """Return the canonical path for the servers config file."""
    env = os.environ.get("TETHER_SERVERS_FILE", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "tether" / "servers.yaml"


def load_servers(path: Path | None = None) -> dict[str, Any]:
    """Load the servers YAML file and return its parsed content.

    Returns an empty dict if the file does not exist or cannot be parsed.
    """
    target = path or default_servers_path()
    if not target.exists():
        return {}
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        # PyYAML is not installed; silently return empty config.
        return {}
    try:
        text = target.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def get_server(name: str, *, path: Path | None = None) -> dict[str, str] | None:
    """Return the server entry for *name*, or None if it does not exist.

    The returned dict may contain ``host``, ``port``, and/or ``token`` keys.
    All values are strings.
    """
    data = load_servers(path=path)
    servers = data.get("servers") or {}
    entry = servers.get(name)
    if not isinstance(entry, dict):
        return None
    # Normalise all values to strings so callers don't have to.
    return {k: str(v) for k, v in entry.items()}


def get_default_server(*, path: Path | None = None) -> dict[str, str] | None:
    """Return the default server entry (from the ``default`` key), or None."""
    data = load_servers(path=path)
    default_name = data.get("default")
    if not default_name:
        return None
    return get_server(str(default_name), path=path)


# ---------------------------------------------------------------------------
# Active context (persistent context switching)
# ---------------------------------------------------------------------------


def default_context_path() -> Path:
    """Return the canonical path for the active context file."""
    env = os.environ.get("TETHER_CONTEXT_FILE", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "tether" / "context"


def get_active_context(*, context_path: Path | None = None) -> str | None:
    """Return the active context name, or None if unset/local.

    Returns None when the file is missing, empty, or contains ``local``.
    """
    target = context_path or default_context_path()
    if not target.exists():
        return None
    try:
        name = target.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not name or name == "local":
        return None
    return name


def set_active_context(
    name: str | None, *, context_path: Path | None = None
) -> None:
    """Set the active context name.

    Pass ``None`` or ``"local"`` to clear the context (removes the file).
    """
    target = context_path or default_context_path()
    if name is None or name == "local":
        if target.exists():
            target.unlink()
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(name + "\n", encoding="utf-8")


def get_active_context_server(
    *,
    context_path: Path | None = None,
    servers_path: Path | None = None,
) -> tuple[str | None, dict[str, str] | None]:
    """Return ``(context_name, server_profile)`` for the active context.

    Returns ``(None, None)`` when no context is active.  Returns
    ``(name, None)`` when a context is set but the server profile does
    not exist in servers.yaml.
    """
    name = get_active_context(context_path=context_path)
    if not name:
        return None, None
    profile = get_server(name, path=servers_path)
    return name, profile


def list_contexts(
    *,
    servers_path: Path | None = None,
    context_path: Path | None = None,
) -> list[dict[str, str]]:
    """Return a list of all available contexts.

    Each entry is a dict with keys ``name``, ``host``, ``port``, and
    ``active`` (``"*"`` if this is the current context, ``""`` otherwise).
    The special ``local`` context is always included as the first entry.
    """
    active = get_active_context(context_path=context_path)
    data = load_servers(path=servers_path)
    servers = data.get("servers") or {}

    result: list[dict[str, str]] = []

    # "local" is always an option
    result.append(
        {
            "name": "local",
            "host": "127.0.0.1",
            "port": "8787",
            "active": "*" if active is None else "",
        }
    )

    for name, entry in sorted(servers.items()):
        if not isinstance(entry, dict):
            continue
        result.append(
            {
                "name": name,
                "host": str(entry.get("host", "?")),
                "port": str(entry.get("port", "8787")),
                "active": "*" if name == active else "",
            }
        )

    return result
