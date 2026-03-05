"""Named remote server profiles for the Tether CLI.

Reads ``~/.config/tether/servers.yaml`` (or the path in
``TETHER_SERVERS_FILE``) and exposes individual server entries so the CLI
can pick a named profile with ``--server <name>``.

Example file::

    servers:
      work:
        host: my-server.local
        port: 8787
        token: secret123
    default: work

If ``default`` is set, that profile is used when no ``--host`` or
``--server`` flag is given on the command line.
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


def write_server(
    name: str,
    entry: dict[str, str],
    *,
    path: Path | None = None,
    set_default: bool = False,
) -> None:
    """Write or update a server entry in servers.yaml.

    Creates the file if it does not exist.  Existing entries for other
    servers are preserved.  If *set_default* is True, the ``default``
    key is updated to *name*.
    """
    target = path or default_servers_path()
    data = load_servers(path=target)

    # Ensure the top-level shape is correct.
    if not isinstance(data.get("servers"), dict):
        data["servers"] = {}

    data["servers"][name] = {k: v for k, v in entry.items()}

    if set_default:
        data["default"] = name

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required to write servers.yaml. "
            "Install it with: pip install pyyaml"
        ) from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
