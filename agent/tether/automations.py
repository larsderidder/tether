"""Automation configuration loading for script-backed sessions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class AutomationError(RuntimeError):
    """Raised when an automation cannot be loaded or used."""


@dataclass(frozen=True)
class AutomationStep:
    """One subprocess step in an automation."""

    name: str
    command: tuple[str, ...]
    cwd: str | None = None


@dataclass(frozen=True)
class Automation:
    """A local script automation that can process a Tether turn."""

    name: str
    description: str = ""
    timeout_seconds: int = 180
    output_markdown: str = "{output_md}"
    steps: tuple[AutomationStep, ...] = ()


def automation_search_dirs(session_directory: str | None = None) -> list[Path]:
    """Return automation directories in lookup order."""

    dirs: list[Path] = [Path.home() / ".config" / "tether" / "automations"]
    if session_directory:
        dirs.append(Path(session_directory).expanduser() / ".tether" / "automations")
    return dirs


def load_automations(session_directory: str | None = None) -> dict[str, Automation]:
    """Load all automations visible to a session."""

    automations: dict[str, Automation] = {}
    for directory in automation_search_dirs(session_directory):
        if not directory.is_dir():
            continue
        for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")]):
            automation = load_automation(path)
            automations[automation.name] = automation
    return automations


def load_automation(path: Path) -> Automation:
    """Load one automation YAML file."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AutomationError(f"Cannot read automation {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AutomationError(f"Automation {path} must contain a mapping.")

    name = str(raw.get("name") or path.stem).strip()
    if not name:
        raise AutomationError(f"Automation {path} has no name.")
    timeout_seconds = _bounded_timeout(raw.get("timeout_seconds"), path)
    steps = _load_steps(raw, path)
    return Automation(
        name=name,
        description=str(raw.get("description") or "").strip(),
        timeout_seconds=timeout_seconds,
        output_markdown=str(
            raw.get("output_markdown") or raw.get("markdown") or "{output_md}"
        ),
        steps=tuple(steps),
    )


def _bounded_timeout(value: Any, path: Path) -> int:
    """Validate an automation timeout."""

    if value is None:
        return 180
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise AutomationError(
            f"Automation {path} timeout_seconds must be an integer."
        ) from exc
    if timeout < 1 or timeout > 900:
        raise AutomationError(
            f"Automation {path} timeout_seconds must be between 1 and 900."
        )
    return timeout


def _load_steps(raw: dict[str, Any], path: Path) -> list[AutomationStep]:
    """Parse automation steps from YAML."""

    raw_steps = raw.get("steps")
    if raw_steps is None and raw.get("command") is not None:
        raw_steps = [
            {
                "name": "run",
                "run": {"command": raw.get("command"), "cwd": raw.get("cwd")},
            }
        ]
    if not isinstance(raw_steps, list) or not raw_steps:
        raise AutomationError(f"Automation {path} must define at least one step.")

    steps: list[AutomationStep] = []
    for index, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            raise AutomationError(f"Automation {path} step {index} must be a mapping.")
        run = item.get("run") if isinstance(item.get("run"), dict) else item
        command = run.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) for part in command)
        ):
            raise AutomationError(
                f"Automation {path} step {index} command must be a non-empty string list."
            )
        shell = run.get("shell", False)
        if shell:
            raise AutomationError(
                f"Automation {path} step {index} uses shell=true, which is not supported."
            )
        steps.append(
            AutomationStep(
                name=str(item.get("name") or run.get("name") or f"step-{index}"),
                command=tuple(command),
                cwd=str(run.get("cwd")).strip() if run.get("cwd") else None,
            )
        )
    return steps
