"""Environment configuration loader for Tether.

Loads settings from layered .env files into ``os.environ``.

Precedence (highest wins):
    1. Already-set environment variables
    2. Local ``.env`` file (cwd)
    3. ``~/.config/tether/config.env`` (XDG_CONFIG_HOME respected)
    4. Built-in defaults
"""

from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    """Return the Tether config directory (XDG_CONFIG_HOME/tether)."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if not base:
        base = os.path.join(Path.home(), ".config")
    return Path(base) / "tether"


def data_dir_default() -> Path:
    """Return the default data directory for installed packages.

    Uses XDG_DATA_HOME/tether (defaults to ~/.local/share/tether).
    """
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    if not base:
        base = os.path.join(Path.home(), ".local", "share")
    return Path(base) / "tether"


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse a .env file and return a dict of key-value pairs.

    Supports:
        - KEY=value
        - KEY="value" and KEY='value' (quotes stripped)
        - export KEY=value
        - # comments and blank lines
        - Inline comments after unquoted values
    """
    result: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Strip optional 'export ' prefix
        if line.startswith("export "):
            line = line[7:].strip()

        # Split on first '='
        eq = line.find("=")
        if eq < 1:
            continue

        key = line[:eq].strip()
        value = line[eq + 1 :].strip()

        # Strip matching quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        else:
            # Remove inline comments for unquoted values
            for i, ch in enumerate(value):
                if ch == "#" and (i == 0 or value[i - 1] == " "):
                    value = value[:i].rstrip()
                    break

        result[key] = value

    return result


def load_config() -> None:
    """Load configuration from .env files into ``os.environ``.

    Files are loaded in reverse precedence order (lowest first) so that
    higher-precedence sources naturally override via dict update, but
    already-set environment variables are never overwritten.
    """
    merged: dict[str, str] = {}

    # Lowest precedence: user config file
    user_config = config_dir() / "config.env"
    merged.update(parse_env_file(user_config))

    # Higher precedence: local .env
    local_env = Path.cwd() / ".env"
    merged.update(parse_env_file(local_env))

    # Apply to os.environ — never overwrite existing vars
    for key, value in merged.items():
        if key not in os.environ:
            os.environ[key] = value
