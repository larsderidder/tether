"""Install bundled agent integration helpers."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True)
class IntegrationSpec:
    """A bundled integration file and its default destination."""

    name: str
    resource: tuple[str, ...]
    destination: tuple[str, ...]
    home_env: str | None = None
    binary: str | None = None


@dataclass(frozen=True)
class IntegrationInstallResult:
    """Result of installing one integration file."""

    name: str
    path: Path
    action: str


_INTEGRATIONS: dict[str, IntegrationSpec] = {
    "pi": IntegrationSpec(
        name="pi",
        resource=("integrations", "pi", "tether-attach.ts"),
        destination=(".pi", "agent", "extensions", "tether-attach.ts"),
        binary="pi",
    ),
    "claude": IntegrationSpec(
        name="claude",
        resource=("integrations", "claude", "commands", "tether.md"),
        destination=("commands", "tether.md"),
        home_env="CLAUDE_HOME",
        binary="claude",
    ),
    "codex": IntegrationSpec(
        name="codex",
        resource=("integrations", "codex", "prompts", "tether.md"),
        destination=("prompts", "tether.md"),
        home_env="CODEX_HOME",
        binary="codex",
    ),
}


def known_integrations() -> list[str]:
    """Return the supported integration names."""
    return sorted(_INTEGRATIONS)


def detected_integrations() -> list[str]:
    """Return integrations for agent CLIs found on PATH."""
    return [
        name
        for name in known_integrations()
        if _INTEGRATIONS[name].binary and shutil.which(_INTEGRATIONS[name].binary)
    ]


def install_integrations(
    targets: list[str] | None = None,
    *,
    force: bool = False,
) -> list[IntegrationInstallResult]:
    """Install bundled integrations into the current user's agent config."""
    selected = detected_integrations() if targets is None else targets
    selected = known_integrations() if "all" in selected else selected
    unknown = sorted(set(selected) - set(_INTEGRATIONS))
    if unknown:
        raise ValueError(f"Unknown integration: {', '.join(unknown)}")

    results: list[IntegrationInstallResult] = []
    for name in selected:
        spec = _INTEGRATIONS[name]
        content = _read_resource(spec.resource)
        target = _target_path(spec)
        action = _write_if_needed(target, content, force=force)
        results.append(IntegrationInstallResult(name=name, path=target, action=action))
    return results


def _read_resource(parts: tuple[str, ...]) -> str:
    """Read a bundled text resource from the tether package."""
    resource = files("tether")
    for part in parts:
        resource = resource.joinpath(part)
    return resource.read_text(encoding="utf-8")


def _target_path(spec: IntegrationSpec) -> Path:
    """Resolve the target path for an integration."""
    if spec.home_env:
        configured_home = os.environ.get(spec.home_env, "").strip()
        if configured_home:
            home = Path(configured_home).expanduser()
        else:
            home = Path.home() / f".{spec.name}"
        return home.joinpath(*spec.destination)
    return Path.home().joinpath(*spec.destination)


def _write_if_needed(path: Path, content: str, *, force: bool) -> str:
    """Write content unless the existing file is different and force is false."""
    existed = path.exists()
    if existed:
        existing = path.read_text(encoding="utf-8")
        if existing == content:
            return "unchanged"
        if not force:
            return "conflict"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return "updated" if existed else "installed"
