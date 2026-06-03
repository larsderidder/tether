"""Runner adapter for local file-based runbooks."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import asdict
from pathlib import Path
from string import Formatter
from uuid import uuid4

import structlog

from tether.bridges.image_io import (
    SUPPORTED_IMAGE_MIME_TYPES,
    detect_image_mime_type,
    sanitize_filename,
)
from tether.runbooks import Runbook, RunbookError, load_runbooks
from tether.runner.base import RunnerEvents
from tether.settings import settings
from tether.store import store

logger = structlog.get_logger(__name__)


class RunbookRunner:
    """Run local runbook subprocesses for each session turn."""

    runner_type = "runbook"

    def __init__(self, events: RunnerEvents) -> None:
        """Create a runbook runner."""

        self._events = events
        self._tasks: dict[str, asyncio.Task] = {}

    async def start(
        self,
        session_id: str,
        prompt: str,
        approval_choice: int,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        """Start a runbook turn."""

        self._start_task(session_id, prompt, images or [])

    async def send_input(
        self,
        session_id: str,
        text: str,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        """Run the configured runbook for follow-up input."""

        self._start_task(session_id, text, images or [])

    async def stop(self, session_id: str) -> int | None:
        """Cancel the active runbook turn, if any."""

        task = self._tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            return 130
        return None

    def update_permission_mode(self, session_id: str, approval_choice: int) -> None:
        """Accept permission-mode updates for runner compatibility."""

    def _start_task(
        self, session_id: str, text: str, images: list[dict[str, str]]
    ) -> None:
        """Schedule a runbook turn without blocking the API request."""

        existing = self._tasks.get(session_id)
        if existing and not existing.done():
            raise RuntimeError("Runbook is already running for this session.")
        task = asyncio.create_task(self._run_turn(session_id, text, images))
        self._tasks[session_id] = task
        task.add_done_callback(lambda done_task: self._tasks.pop(session_id, None))

    async def _run_turn(
        self, session_id: str, text: str, images: list[dict[str, str]]
    ) -> None:
        """Run one turn and report output back through Tether."""

        try:
            session = store.get_session(session_id)
            if not session:
                return
            runbook, clean_text = self._select_runbook(session.directory, text)
            run_dir = self._create_run_dir(session_id, runbook.name)
            image_rows = self._write_images(run_dir / "input", images)
            manifest = self._write_manifest(
                run_dir, session_id, runbook, clean_text, image_rows
            )
            output = await self._execute_runbook(
                runbook, run_dir, manifest, session.directory
            )
            await self._events.on_output(
                session_id, "combined", output, kind="final", is_final=True
            )
            await self._events.on_awaiting_input(session_id)
        except asyncio.CancelledError:
            await self._events.on_error(
                session_id, "RUNBOOK_CANCELLED", "Runbook was cancelled."
            )
            raise
        except Exception as exc:
            logger.exception("Runbook turn failed", session_id=session_id)
            await self._events.on_error(session_id, "RUNBOOK_ERROR", str(exc))

    def _select_runbook(
        self, session_directory: str | None, text: str
    ) -> tuple[Runbook, str]:
        """Select a runbook from the session directory and optional first token."""

        runbooks = load_runbooks(session_directory)
        if not runbooks:
            raise RunbookError(
                "No runbooks found. Add .tether/runbooks/*.yaml in the session directory."
            )

        stripped = text.strip()
        first, _, rest = stripped.partition(" ")
        explicit = (
            first.removeprefix("/runbook:") if first.startswith("/runbook:") else first
        )
        if explicit in runbooks and rest:
            return runbooks[explicit], rest.strip()
        if explicit in runbooks and not rest:
            return runbooks[explicit], ""
        if len(runbooks) == 1:
            return next(iter(runbooks.values())), text
        available = ", ".join(sorted(runbooks))
        raise RunbookError(
            f"Choose a runbook as the first word of your message. Available: {available}."
        )

    def _create_run_dir(self, session_id: str, runbook_name: str) -> Path:
        """Create a private run directory for one invocation."""

        safe_session = (
            "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in session_id)[
                :120
            ]
            or "session"
        )
        safe_runbook = (
            "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in runbook_name)[
                :80
            ]
            or "runbook"
        )
        run_dir = (
            Path(settings.data_dir())
            / "runbook-runs"
            / safe_session
            / f"{safe_runbook}-{uuid4().hex}"
        )
        (run_dir / "input").mkdir(parents=True, exist_ok=False)
        return run_dir

    def _write_images(
        self, input_dir: Path, images: list[dict[str, str]]
    ) -> list[dict[str, object]]:
        """Decode API image payloads into files under the run directory."""

        rows: list[dict[str, object]] = []
        for index, image in enumerate(images, start=1):
            data = str(image.get("data") or "")
            mime_type = (
                str(image.get("mimeType") or image.get("mime_type") or "")
                .split(";", 1)[0]
                .lower()
            )
            decoded = base64.b64decode(data, validate=True)
            if detect_image_mime_type(decoded) != mime_type:
                raise RunbookError("Image payload failed validation.")
            suffix = SUPPORTED_IMAGE_MIME_TYPES[mime_type]
            fallback = f"image-{index:03d}.{suffix}"
            filename = (
                sanitize_filename(
                    str(image.get("filename") or fallback), mime_type=mime_type
                )
                or fallback
            )
            path = input_dir / f"{index:03d}-{Path(filename).name}"
            path.write_bytes(decoded)
            rows.append(
                {
                    "path": str(path),
                    "filename": filename,
                    "mime_type": mime_type,
                    "size": len(decoded),
                }
            )
        return rows

    def _write_manifest(
        self,
        run_dir: Path,
        session_id: str,
        runbook: Runbook,
        text: str,
        images: list[dict[str, object]],
    ) -> Path:
        """Write the JSON manifest consumed by runbook scripts."""

        manifest = {
            "session_id": session_id,
            "run_id": run_dir.name,
            "runbook": runbook.name,
            "text": text,
            "run_dir": str(run_dir),
            "input_dir": str(run_dir / "input"),
            "output_md": str(run_dir / "output.md"),
            "output_json": str(run_dir / "output.json"),
            "images": images,
            "steps": [asdict(step) for step in runbook.steps],
        }
        path = run_dir / "manifest.json"
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    async def _execute_runbook(
        self,
        runbook: Runbook,
        run_dir: Path,
        manifest: Path,
        session_directory: str | None,
    ) -> str:
        """Execute runbook steps and return the Markdown result."""

        context = {
            "manifest": str(manifest),
            "run_dir": str(run_dir),
            "input_dir": str(run_dir / "input"),
            "output_md": str(run_dir / "output.md"),
            "output_json": str(run_dir / "output.json"),
        }
        log_path = run_dir / "run.log"
        stdout_text = ""
        for step in runbook.steps:
            command = [render_template(part, context) for part in step.command]
            cwd = self._resolve_cwd(step.cwd, session_directory)
            with log_path.open("a", encoding="utf-8") as log:
                log.write("$ " + " ".join(command) + "\n")
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=runbook.timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                proc.kill()
                await proc.communicate()
                raise RunbookError(
                    f"Runbook timed out after {runbook.timeout_seconds} seconds."
                ) from exc
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            with log_path.open("a", encoding="utf-8") as log:
                log.write(stdout_text)
                log.write(stderr_text)
                log.write(f"\nexit={proc.returncode}\n")
            if proc.returncode != 0:
                detail = (
                    stderr_text.strip()
                    or stdout_text.strip()
                    or f"exit {proc.returncode}"
                )
                raise RunbookError(f"Runbook step {step.name} failed: {detail[:1200]}")

        output_path = Path(render_template(runbook.output_markdown, context))
        if output_path.exists() and output_path.is_file():
            text = output_path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
        if stdout_text.strip():
            return stdout_text.strip()
        return f"Runbook `{runbook.name}` completed. Run directory: `{run_dir}`"

    def _resolve_cwd(self, raw_cwd: str | None, session_directory: str | None) -> Path:
        """Resolve a configured working directory."""

        if raw_cwd:
            cwd = Path(raw_cwd).expanduser()
        elif session_directory:
            cwd = Path(session_directory).expanduser()
        else:
            cwd = Path.cwd()
        if not cwd.is_dir():
            raise RunbookError(f"Runbook cwd does not exist: {cwd}")
        return cwd.resolve()


def render_template(template: str, values: dict[str, str]) -> str:
    """Render a small allowlisted format string."""

    used_names = [
        field_name for _, field_name, _, _ in Formatter().parse(template) if field_name
    ]
    unknown = sorted({name for name in used_names if name not in values})
    if unknown:
        raise RunbookError(f"Unknown runbook template field: {', '.join(unknown)}")
    return template.format_map(values)
