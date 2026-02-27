"""Session template loading and resolution.

Templates are YAML files that describe a repeatable session configuration.
They are looked up by name (filename without extension) in:

  1. An explicit path passed directly (absolute or relative)
  2. ``.tether/templates/`` in the current working directory
  3. ``~/.config/tether/templates/``

Example template file::

    # ~/.config/tether/templates/my-project.yaml
    name: "Fix issues on my-project"
    clone_url: git@github.com:user/my-project.git
    branch: main
    adapter: claude_auto
    approval_mode: 2
    platform: telegram
    auto_branch: true
    auto_checkpoint: true
    shallow: false

Any field in the template can be overridden by explicit CLI/API flags at
call time.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Template schema helpers
# ---------------------------------------------------------------------------

# All recognised top-level keys in a template file.
TEMPLATE_KEYS = {
    "name",
    "clone_url",
    "branch",           # maps to clone_branch
    "adapter",
    "approval_mode",
    "platform",
    "auto_branch",
    "auto_checkpoint",
    "shallow",
    "directory",
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _search_dirs(cwd: str | None = None) -> list[Path]:
    """Return ordered list of directories to search for templates."""
    dirs: list[Path] = []
    base = Path(cwd) if cwd else Path.cwd()
    dirs.append(base / ".tether" / "templates")
    config_home = os.environ.get("XDG_CONFIG_HOME", "")
    if config_home:
        dirs.append(Path(config_home) / "tether" / "templates")
    else:
        dirs.append(Path.home() / ".config" / "tether" / "templates")
    return dirs


def list_templates(cwd: str | None = None) -> list[dict[str, str]]:
    """Return all available templates across all search directories.

    Each entry has ``name`` (stem), ``path`` (absolute path), and
    ``source`` (human-readable directory label).
    """
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for directory in _search_dirs(cwd):
        if not directory.is_dir():
            continue
        for f in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
            name = f.stem
            if name in seen:
                continue
            seen.add(name)
            results.append({"name": name, "path": str(f), "source": str(directory)})
    return results


def find_template(name_or_path: str, cwd: str | None = None) -> Path | None:
    """Locate a template file by name or explicit path.

    If ``name_or_path`` looks like a path (contains ``/`` or ends with
    ``.yaml``/``.yml``), it is treated as a literal file path. Otherwise
    the search directories are scanned for ``<name>.yaml`` or
    ``<name>.yml``.

    Returns the resolved :class:`Path` or ``None`` if not found.
    """
    candidate = Path(name_or_path).expanduser()
    if "/" in name_or_path or name_or_path.endswith((".yaml", ".yml")):
        return candidate if candidate.exists() else None

    for directory in _search_dirs(cwd):
        for ext in (".yaml", ".yml"):
            p = directory / f"{name_or_path}{ext}"
            if p.exists():
                return p
    return None


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------


def load_template(path: Path) -> dict[str, Any]:
    """Parse a template YAML file and return its contents.

    Raises :class:`TemplateError` on parse failures or unknown keys.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise TemplateError(
            "PyYAML is required for template support. "
            "Install it with: pip install pyyaml"
        ) from exc

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateError(f"Cannot read template file {path}: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TemplateError(f"Invalid YAML in {path}: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TemplateError(f"Template {path} must be a YAML mapping, got {type(data).__name__}")

    unknown = set(data.keys()) - TEMPLATE_KEYS
    if unknown:
        raise TemplateError(
            f"Unknown key(s) in template {path}: {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(TEMPLATE_KEYS))}"
        )

    return data


# ---------------------------------------------------------------------------
# Resolution: merge template with caller overrides
# ---------------------------------------------------------------------------


def resolve_template(
    name_or_path: str,
    overrides: dict[str, Any] | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Load a template and apply caller overrides on top.

    Returns a dict with keys that map directly to ``CreateSessionRequest``
    fields: ``clone_url``, ``clone_branch``, ``adapter``, ``platform``,
    ``auto_branch``, ``shallow``, ``directory``.

    The ``approval_mode`` and ``auto_checkpoint`` fields are also included
    when present.

    Caller-supplied overrides (e.g. explicit CLI flags) win over template
    values. A ``None`` override is treated as "not set" and the template
    value is kept.
    """
    path = find_template(name_or_path, cwd=cwd)
    if path is None:
        raise TemplateError(
            f"Template '{name_or_path}' not found. "
            "Run 'tether templates list' to see available templates."
        )
    raw = load_template(path)

    # Normalise: 'branch' -> 'clone_branch'
    if "branch" in raw and "clone_branch" not in raw:
        raw["clone_branch"] = raw.pop("branch")

    merged: dict[str, Any] = {
        "clone_url": raw.get("clone_url"),
        "clone_branch": raw.get("clone_branch"),
        "adapter": raw.get("adapter"),
        "platform": raw.get("platform"),
        "auto_branch": raw.get("auto_branch", False),
        "shallow": raw.get("shallow", False),
        "directory": raw.get("directory"),
        "approval_mode": raw.get("approval_mode"),
        "auto_checkpoint": raw.get("auto_checkpoint"),
        "template_name": raw.get("name"),
    }

    # Apply overrides: only non-None values win
    for key, value in (overrides or {}).items():
        if value is not None:
            merged[key] = value

    return merged


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TemplateError(Exception):
    """Raised when a template cannot be loaded or resolved."""
