"""Runbook configuration loading for script-backed sessions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class RunbookError(RuntimeError):
    """Raised when a runbook cannot be loaded or used."""


@dataclass(frozen=True)
class RunbookStep:
    """One subprocess step in a runbook."""

    name: str
    command: tuple[str, ...]
    cwd: str | None = None


@dataclass(frozen=True)
class Runbook:
    """A local runbook that can process a Tether turn."""

    name: str
    description: str = ""
    timeout_seconds: int = 180
    output_markdown: str = "{output_md}"
    steps: tuple[RunbookStep, ...] = ()


def runbook_search_dirs(session_directory: str | None = None) -> list[Path]:
    """Return runbook directories in lookup order."""

    dirs: list[Path] = []
    if session_directory:
        dirs.append(Path(session_directory).expanduser() / ".tether" / "runbooks")
    dirs.append(Path.home() / ".config" / "tether" / "runbooks")
    return dirs


def load_runbooks(session_directory: str | None = None) -> dict[str, Runbook]:
    """Load all runbooks visible to a session."""

    runbooks: dict[str, Runbook] = {}
    for directory in runbook_search_dirs(session_directory):
        if not directory.is_dir():
            continue
        for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
            runbook = load_runbook(path)
            runbooks[runbook.name] = runbook
    return runbooks


def load_runbook(path: Path) -> Runbook:
    """Load one runbook YAML file."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RunbookError(f"Cannot read runbook {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RunbookError(f"Runbook {path} must contain a mapping.")

    name = str(raw.get("name") or path.stem).strip()
    if not name:
        raise RunbookError(f"Runbook {path} has no name.")
    timeout_seconds = _bounded_timeout(raw.get("timeout_seconds"), path)
    steps = _load_steps(raw, path)
    return Runbook(
        name=name,
        description=str(raw.get("description") or "").strip(),
        timeout_seconds=timeout_seconds,
        output_markdown=str(
            raw.get("output_markdown") or raw.get("markdown") or "{output_md}"
        ),
        steps=tuple(steps),
    )


def _bounded_timeout(value: Any, path: Path) -> int:
    """Validate a runbook timeout."""

    if value is None:
        return 180
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise RunbookError(
            f"Runbook {path} timeout_seconds must be an integer."
        ) from exc
    if timeout < 1 or timeout > 900:
        raise RunbookError(f"Runbook {path} timeout_seconds must be between 1 and 900.")
    return timeout


def _load_steps(raw: dict[str, Any], path: Path) -> list[RunbookStep]:
    """Parse runbook steps from YAML."""

    raw_steps = raw.get("steps")
    if raw_steps is None and raw.get("command") is not None:
        raw_steps = [
            {
                "name": "run",
                "run": {"command": raw.get("command"), "cwd": raw.get("cwd")},
            }
        ]
    if not isinstance(raw_steps, list) or not raw_steps:
        raise RunbookError(f"Runbook {path} must define at least one step.")

    steps: list[RunbookStep] = []
    for index, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            raise RunbookError(f"Runbook {path} step {index} must be a mapping.")
        run = item.get("run") if isinstance(item.get("run"), dict) else item
        command = run.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) for part in command)
        ):
            raise RunbookError(
                f"Runbook {path} step {index} command must be a non-empty string list."
            )
        shell = run.get("shell", False)
        if shell:
            raise RunbookError(
                f"Runbook {path} step {index} uses shell=true, which is not supported."
            )
        steps.append(
            RunbookStep(
                name=str(item.get("name") or run.get("name") or f"step-{index}"),
                command=tuple(command),
                cwd=str(run.get("cwd")).strip() if run.get("cwd") else None,
            )
        )
    return steps
