"""Shared directory history and session configuration notices for bridges."""

from __future__ import annotations

import asyncio
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from agent_tether.base import BridgeCallbacks, BridgeInterface

from tether.adapter_names import (
    adapter_label,
    bridge_agent_to_adapter,
    normalize_adapter_name,
)
from tether.settings import settings

logger = structlog.get_logger(__name__)


async def recent_directories(
    callbacks: BridgeCallbacks, *, current_directory: str | None = None
) -> list[str]:
    """List existing workspaces by activity, with the current directory first."""
    sessions = list(await callbacks.list_sessions())
    if current_directory:
        sessions.append({"directory": current_directory})
    try:
        external = await asyncio.wait_for(
            callbacks.list_external_sessions(limit=200), timeout=10
        )
        sessions.extend(external)
    except Exception:
        logger.exception("Could not load external directory history")

    def collect() -> list[str]:
        """Resolve paths off the event loop and collapse duplicate workspaces."""
        by_directory: dict[str, float] = {}
        for session in sessions:
            raw = session.get("directory")
            if not raw:
                continue
            try:
                path = Path(raw).expanduser().resolve()
                if not path.is_dir():
                    continue
            except (OSError, RuntimeError, ValueError):
                continue

            timestamp = session.get("last_activity_at") or session.get("last_activity")
            timestamp = timestamp or session.get("created_at") or ""
            try:
                date = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                activity = date.timestamp()
            except ValueError:
                activity = 0.0
            directory = str(path)
            by_directory[directory] = max(by_directory.get(directory, 0.0), activity)

        directories = sorted(by_directory, key=lambda path: (-by_directory[path], path))
        if current_directory:
            try:
                current = str(Path(current_directory).expanduser().resolve())
                if current in directories:
                    directories.remove(current)
                    directories.insert(0, current)
            except (OSError, RuntimeError, ValueError):
                pass
        return directories[:50]

    return await asyncio.to_thread(collect)


class SessionCreationMixin(BridgeInterface):
    """Apply Tether's new-session defaults consistently across all bridges."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Give upstream naming and command paths the same canonical default."""
        super().__init__(*args, **kwargs)
        self._config.default_adapter = normalize_adapter_name(
            self._config.default_adapter or settings.adapter()
        )

    async def _parse_new_args(
        self, args: str, *, base_session_id: str | None
    ) -> tuple[str | None, str]:
        """Reuse the parent's directory, never its adapter implicitly."""
        parts = (args or "").split(maxsplit=1)
        adapter = self._config.default_adapter
        directory_raw = (args or "").strip()
        if parts and self._agent_to_adapter(parts[0]):
            adapter = self._agent_to_adapter(parts[0])
            directory_raw = parts[1] if len(parts) > 1 else ""

        base = (
            self._get_session_info(base_session_id)
            if base_session_id and self._get_session_info
            else None
        )
        base_directory = (base or {}).get("directory")
        directory_raw = directory_raw or base_directory or ""
        if not directory_raw:
            raise ValueError(
                "Usage: !new <agent> <directory> (default: Pi; also Claude, Codex, OpenCode)"
            )
        directory = await self._resolve_directory_arg(
            directory_raw, base_directory=base_directory
        )
        return adapter, directory

    def _model_for_child_session(
        self, base_session_id: str | None, adapter: str | None
    ) -> str | None:
        """Reuse an explicitly selected parent's model only while it remains allowed."""
        if not base_session_id or not adapter or not self._get_session_info:
            return None
        base = self._get_session_info(base_session_id) or {}
        if normalize_adapter_name(base.get("adapter")) != normalize_adapter_name(
            adapter
        ):
            return None
        model = settings.normalize_adapter_model(adapter, base.get("model"))
        if not model or settings.is_adapter_model_blocked(adapter, model):
            return None

        available_models = settings.adapter_models(adapter)
        if available_models and model not in available_models:
            return None
        return model

    def _parse_new_args_extended(self, args: str) -> dict:
        """Parse Tether's workspace and template flags independently of bridges."""
        tokens = iter(shlex.split(args or ""))
        parsed: dict = {
            "clone_url": None,
            "template": None,
            "clone_branch": None,
            "adapter": None,
            "message": None,
            "auto_branch": False,
            "shallow": False,
            "directory_raw": None,
        }
        value_flags = {
            "--clone": "clone_url",
            "-c": "clone_url",
            "--template": "template",
            "-t": "template",
            "--branch": "clone_branch",
            "-b": "clone_branch",
            "--adapter": "adapter",
            "-a": "adapter",
            "--message": "message",
            "-m": "message",
        }
        positional: list[str] = []
        for token in tokens:
            if token in value_flags:
                value = next(tokens, None)
                if value is None or value.startswith("-"):
                    raise ValueError(f"{token} requires a value")
                key = value_flags[token]
                if key == "adapter":
                    adapter = bridge_agent_to_adapter(value)
                    if adapter is None:
                        raise ValueError(f"Unknown adapter: {value}")
                    parsed[key] = adapter
                else:
                    parsed[key] = value
            elif token in ("--auto-branch", "--shallow"):
                parsed[token[2:].replace("-", "_")] = True
            elif token.startswith("-"):
                raise ValueError(f"Unknown flag: {token}")
            else:
                positional.append(token)
        if parsed["clone_url"] and parsed["template"]:
            raise ValueError("--clone and --template are mutually exclusive")
        if positional:
            parsed["directory_raw"] = " ".join(positional)
        return parsed

    async def _handle_new_extended(
        self,
        event_ctx: object,
        parsed: dict,
        *,
        platform: str,
        base_session_id: str | None = None,
    ) -> str:
        """Create a Tether workspace without inheriting the parent agent/model."""
        callbacks = self._callbacks
        if callbacks is None:
            return "Failed to create session: bridge callbacks are not configured"
        adapter = parsed.get("adapter")
        template = parsed.get("template")
        if not adapter and not template:
            adapter = self._config.default_adapter
        kwargs: dict = {"platform": platform}
        if adapter:
            kwargs["adapter"] = adapter
        clone_url = parsed.get("clone_url")
        directory_raw = parsed.get("directory_raw")
        try:
            if clone_url:
                kwargs.update(
                    clone_url=clone_url,
                    clone_branch=parsed.get("clone_branch"),
                    shallow=bool(parsed.get("shallow")),
                    auto_branch=bool(parsed.get("auto_branch")),
                )
            elif template:
                kwargs["template"] = template
                for key in ("clone_branch", "shallow", "auto_branch"):
                    if parsed.get(key):
                        kwargs[key] = parsed[key]
            elif directory_raw:
                kwargs["directory"] = await self._resolve_directory_arg(
                    directory_raw, base_directory=None
                )
            else:
                return "Usage: !new --clone <url> | --template <name> | [agent] [directory]"
            session = await self._create_session_via_api(**kwargs)
        except Exception as exc:
            return f"Failed to create session: {exc}"

        label = adapter_label(
            adapter or session.get("adapter") or self._config.default_adapter
        )
        if clone_url:
            name = clone_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
            result = f"✅ Cloned {name} and started {label} session."
        elif template:
            name = session.get("name") or template
            result = f"✅ Started {label} session from template '{name}'."
        else:
            name = Path(kwargs["directory"]).name or "Session"
            result = f"✅ New {label} session created in {name}."
        branch = session.get("working_branch") or parsed.get("clone_branch")
        if branch:
            result += f" Branch: `{branch}`."
        if parsed.get("message"):
            try:
                await callbacks.send_input(session["id"], parsed["message"])
            except Exception as exc:
                result += f"\n⚠️ Failed to start with prompt: {exc}"
        return result

    async def _create_session_via_api(self, **kwargs: Any) -> dict:
        """Create once; a failed notice must not turn success into a retry."""
        callbacks = self._callbacks
        if callbacks is None:
            raise RuntimeError("Bridge callbacks are not configured")
        # Workspace/template options belong to the host callback contract, not
        # the published bridge base class's narrower creation signature.
        session = await callbacks.create_session(
            **{key: value for key, value in kwargs.items() if value is not None}
        )
        adapter = session.get("adapter") or kwargs.get("adapter") or settings.adapter()
        model = session.get("model") or settings.adapter_default_model(adapter)
        label = adapter_label(adapter) or adapter
        notice = (
            f"Agent: {label}\n" f"Model: {model or 'agent default (not reported yet)'}"
        )
        try:
            await self.on_output(session["id"], notice)
        except Exception:
            logger.exception(
                "Could not announce session configuration", session_id=session.get("id")
            )
        return session
