"""Runner adapter for local script automations."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from string import Formatter
from uuid import uuid4

import structlog

from tether.automations import Automation, AutomationError, load_automations
from tether.bridges.image_io import (
    SUPPORTED_IMAGE_MIME_TYPES,
    detect_image_mime_type,
    sanitize_filename,
)
from tether.runner.base import RunnerEvents
from tether.settings import settings
from tether.store import store

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AutomationTurn:
    """Queued automation input turn."""

    text: str
    images: list[dict[str, str]]


class AutomationRunner:
    """Run local automation subprocesses for each session turn."""

    runner_type = "automation"

    def __init__(self, events: RunnerEvents) -> None:
        """Create an automation runner."""

        self._events = events
        self._tasks: dict[str, asyncio.Task] = {}
        self._queues: dict[str, asyncio.Queue[AutomationTurn]] = {}

    async def start(
        self,
        session_id: str,
        prompt: str,
        approval_choice: int,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        """Start an automation turn."""

        self._start_task(session_id, prompt, images or [])

    async def send_input(
        self,
        session_id: str,
        text: str,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        """Run the configured automation for follow-up input."""

        self._start_task(session_id, text, images or [])

    async def stop(self, session_id: str) -> int | None:
        """Cancel the active automation turn, if any."""

        task = self._tasks.pop(session_id, None)
        queue = self._queues.pop(session_id, None)
        if queue:
            while not queue.empty():
                queue.get_nowait()
                queue.task_done()
        if task and not task.done():
            task.cancel()
            return 130
        return None

    def update_permission_mode(self, session_id: str, approval_choice: int) -> None:
        """Accept permission-mode updates for runner compatibility."""

    def _start_task(
        self, session_id: str, text: str, images: list[dict[str, str]]
    ) -> None:
        """Schedule an automation turn without blocking the API request."""

        queue = self._queues.setdefault(session_id, asyncio.Queue())
        queue.put_nowait(AutomationTurn(text=text, images=images))
        existing = self._tasks.get(session_id)
        if existing and not existing.done():
            logger.info(
                "Queued automation turn",
                session_id=session_id,
                queue_size=queue.qsize(),
            )
            return
        task = asyncio.create_task(self._run_queue(session_id))
        self._tasks[session_id] = task
        task.add_done_callback(
            lambda done_task, sid=session_id: self._clear_task(sid, done_task)
        )

    def _clear_task(self, session_id: str, task: asyncio.Task) -> None:
        """Remove a task only if it is still the active task for the session."""

        if self._tasks.get(session_id) is task:
            self._tasks.pop(session_id, None)

    async def _run_queue(self, session_id: str) -> None:
        """Run queued turns sequentially for one session."""

        try:
            queue = self._queues[session_id]
            while True:
                turn = await queue.get()
                try:
                    await self._run_turn(session_id, turn.text, turn.images)
                finally:
                    queue.task_done()

                if queue.empty():
                    await self._events.on_awaiting_input(session_id)
                    if queue.empty():
                        break
        except asyncio.CancelledError:
            await self._events.on_error(
                session_id, "AUTOMATION_CANCELLED", "Automation was cancelled."
            )
            raise
        except Exception as exc:
            logger.exception("Automation turn failed", session_id=session_id)
            await self._events.on_error(session_id, "AUTOMATION_ERROR", str(exc))
        finally:
            queue = self._queues.get(session_id)
            if queue is not None and queue.empty():
                self._queues.pop(session_id, None)

    async def _run_turn(
        self, session_id: str, text: str, images: list[dict[str, str]]
    ) -> None:
        """Run one turn and report output back through Tether."""

        session = store.get_session(session_id)
        if not session:
            return
        automation, clean_text = self._select_automation(session.directory, text)
        run_dir = self._create_run_dir(session_id, automation.name)
        image_rows = self._write_images(run_dir / "input", images)
        manifest = self._write_manifest(
            run_dir, session_id, automation, clean_text, image_rows
        )
        output = await self._execute_automation(
            session_id, automation, run_dir, manifest, session.directory
        )
        await self._events.on_output(
            session_id, "combined", output, kind="final", is_final=True
        )

    def _select_automation(
        self, session_directory: str | None, text: str
    ) -> tuple[Automation, str]:
        """Select an automation from the session directory and optional first token."""

        automations = load_automations(session_directory)
        if not automations:
            raise AutomationError(
                "No automations found. Add .tether/automations/*.yaml in the session directory."
            )

        stripped = text.strip()
        first, _, rest = stripped.partition(" ")
        explicit = (
            first.removeprefix("/automation:")
            if first.startswith("/automation:")
            else first
        )
        if explicit in automations and rest:
            return automations[explicit], rest.strip()
        if explicit in automations and not rest:
            return automations[explicit], ""
        if len(automations) == 1:
            return next(iter(automations.values())), text
        available = ", ".join(sorted(automations))
        raise AutomationError(
            f"Choose an automation as the first word of your message. Available: {available}."
        )

    def _create_run_dir(self, session_id: str, automation_name: str) -> Path:
        """Create a private run directory for one invocation."""

        safe_session = (
            "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in session_id)[
                :120
            ]
            or "session"
        )
        safe_automation = (
            "".join(
                ch if ch.isalnum() or ch in "._-" else "_" for ch in automation_name
            )[:80]
            or "automation"
        )
        run_dir = (
            Path(settings.data_dir())
            / "automation-runs"
            / safe_session
            / f"{safe_automation}-{uuid4().hex}"
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
                raise AutomationError("Image payload failed validation.")
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
        automation: Automation,
        text: str,
        images: list[dict[str, object]],
    ) -> Path:
        """Write the JSON manifest consumed by automation scripts."""

        manifest = {
            "session_id": session_id,
            "run_id": run_dir.name,
            "automation": automation.name,
            "text": text,
            "run_dir": str(run_dir),
            "input_dir": str(run_dir / "input"),
            "output_md": str(run_dir / "output.md"),
            "output_json": str(run_dir / "output.json"),
            "output_messages_dir": str(run_dir / "messages"),
            "images": images,
            "steps": [asdict(step) for step in automation.steps],
        }
        path = run_dir / "manifest.json"
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    async def _execute_automation(
        self,
        session_id: str,
        automation: Automation,
        run_dir: Path,
        manifest: Path,
        session_directory: str | None,
    ) -> str:
        """Execute automation steps and return the Markdown result."""

        context = {
            "manifest": str(manifest),
            "run_dir": str(run_dir),
            "input_dir": str(run_dir / "input"),
            "output_md": str(run_dir / "output.md"),
            "output_json": str(run_dir / "output.json"),
            "output_messages_dir": str(run_dir / "messages"),
        }
        log_path = run_dir / "run.log"
        stdout_text = ""
        messages_seen: set[Path] = set()
        messages_dir = run_dir / "messages"
        for step in automation.steps:
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
            stop_streaming = asyncio.Event()
            stream_task = asyncio.create_task(
                self._stream_output_messages(
                    session_id, messages_dir, messages_seen, stop_streaming
                )
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=automation.timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                proc.kill()
                await proc.communicate()
                raise AutomationError(
                    f"Automation timed out after {automation.timeout_seconds} seconds."
                ) from exc
            finally:
                stop_streaming.set()
                await stream_task
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
                raise AutomationError(
                    f"Automation step {step.name} failed: {detail[:1200]}"
                )

        output_path = Path(render_template(automation.output_markdown, context))
        if output_path.exists() and output_path.is_file():
            text = output_path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
        if stdout_text.strip():
            return stdout_text.strip()
        return f"Automation `{automation.name}` completed. Run directory: `{run_dir}`"

    async def _stream_output_messages(
        self,
        session_id: str,
        messages_dir: Path,
        seen: set[Path],
        stop: asyncio.Event,
    ) -> None:
        """Send automation message files as soon as they appear."""

        while True:
            await self._send_new_output_messages(session_id, messages_dir, seen)
            if stop.is_set():
                await self._send_new_output_messages(session_id, messages_dir, seen)
                return
            await asyncio.sleep(0.25)

    async def _send_new_output_messages(
        self, session_id: str, messages_dir: Path, seen: set[Path]
    ) -> None:
        """Send new per-message Markdown outputs from an automation."""

        if not messages_dir.is_dir():
            return
        for path in sorted(messages_dir.glob("*.md")):
            if path in seen or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                continue
            seen.add(path)
            await self._events.on_output(
                session_id,
                "combined",
                text,
                kind="step",
                is_final=False,
                bridge_segments=[{"kind": "automation_message", "text": text}],
            )

    def _resolve_cwd(self, raw_cwd: str | None, session_directory: str | None) -> Path:
        """Resolve a configured working directory."""

        if raw_cwd:
            cwd = Path(raw_cwd).expanduser()
        elif session_directory:
            cwd = Path(session_directory).expanduser()
        else:
            cwd = Path.cwd()
        if not cwd.is_dir():
            raise AutomationError(f"Automation cwd does not exist: {cwd}")
        return cwd.resolve()


def render_template(template: str, values: dict[str, str]) -> str:
    """Render a small allowlisted format string."""

    used_names = [
        field_name for _, field_name, _, _ in Formatter().parse(template) if field_name
    ]
    unknown = sorted({name for name in used_names if name not in values})
    if unknown:
        raise AutomationError(
            f"Unknown automation template field: {', '.join(unknown)}"
        )
    return template.format_map(values)
