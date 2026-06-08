"""Tests for the automation runner adapter."""

from __future__ import annotations

import asyncio
import base64
import sys

import pytest

from tether.automations import AutomationError, load_automation, load_automations
from tether.bridges.image_io import images_from_payload
from tether.models import SessionState
from tether.runner.automation import AutomationRunner, AutomationTurn, render_template

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 16)


class FakeRunnerEvents:
    """Fake event sink for automation runner tests."""

    def __init__(self) -> None:
        """Create an empty event sink."""

        self.outputs: list[dict] = []
        self.errors: list[dict] = []
        self.awaiting_input_count = 0
        self.on_awaiting_input_hook = None

    async def on_output(
        self,
        session_id,
        stream,
        text,
        *,
        kind="final",
        is_final=None,
        bridge_segments=None,
    ):
        """Record runner output."""

        self.outputs.append(
            {
                "session_id": session_id,
                "stream": stream,
                "text": text,
                "kind": kind,
                "is_final": is_final,
                "bridge_segments": bridge_segments,
            }
        )

    async def on_error(self, session_id, code, message):
        """Record runner errors."""

        self.errors.append({"session_id": session_id, "code": code, "message": message})

    async def on_awaiting_input(self, session_id):
        """Record idle transitions."""

        self.awaiting_input_count += 1
        if self.on_awaiting_input_hook:
            await self.on_awaiting_input_hook()


@pytest.mark.anyio
async def test_automation_runner_saves_images_and_returns_markdown(
    tmp_path, fresh_store, monkeypatch
) -> None:
    """An automation turn writes a manifest, image files, and returns output.md."""

    import tether.runner.automation as automation_module

    monkeypatch.setattr(automation_module, "store", fresh_store)
    monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

    workspace = tmp_path / "workspace"
    automations = workspace / ".tether" / "automations"
    automations.mkdir(parents=True)
    script = workspace / "triage.py"
    script.write_text(
        """
from __future__ import annotations

import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
Path(manifest["output_md"]).write_text(
    f"# Done\\nAutomation: {manifest['automation']}\\nImages: {len(manifest['images'])}\\nText: {manifest['text']}\\n",
    encoding="utf-8",
)
""".lstrip(),
        encoding="utf-8",
    )
    (automations / "pokemon.yaml").write_text(
        """
name: pokemon
steps:
  - name: triage
    run:
      command:
        - python
        - triage.py
        - "{manifest}"
""".lstrip().replace(
            "python", sys.executable
        ),
        encoding="utf-8",
    )

    session = fresh_store.create_session("repo", None)
    fresh_store.set_workdir(session.id, str(workspace), managed=False)
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)

    events = FakeRunnerEvents()
    runner = AutomationRunner(events)
    image_payload = {
        "type": "image",
        "data": base64.b64encode(PNG_BYTES).decode("ascii"),
        "mimeType": "image/png",
        "filename": "card.png",
    }

    await runner.send_input(
        session.id, "check this", images=images_from_payload([image_payload])
    )
    task = runner._tasks[session.id]
    await task

    assert events.errors == []
    assert events.awaiting_input_count == 1
    assert (
        events.outputs[0]["text"]
        == "# Done\nAutomation: pokemon\nImages: 1\nText: check this"
    )
    image_files = list(
        (tmp_path / "data" / "automation-runs" / session.id).glob(
            "pokemon-*/input/*.png"
        )
    )
    assert len(image_files) == 1
    assert image_files[0].read_bytes() == PNG_BYTES


@pytest.mark.anyio
async def test_automation_runner_queues_overlapping_turns(
    tmp_path, fresh_store, monkeypatch
) -> None:
    """Overlapping automation inputs are processed sequentially instead of rejected."""

    import tether.runner.automation as automation_module

    monkeypatch.setattr(automation_module, "store", fresh_store)
    monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

    workspace = tmp_path / "workspace"
    automations = workspace / ".tether" / "automations"
    automations.mkdir(parents=True)
    script = workspace / "triage.py"
    script.write_text(
        """
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
time.sleep(0.05)
Path(manifest["output_md"]).write_text(manifest["text"], encoding="utf-8")
""".lstrip(),
        encoding="utf-8",
    )
    (automations / "pokemon.yaml").write_text(
        """
name: pokemon
steps:
  - name: triage
    run:
      command:
        - python
        - triage.py
        - "{manifest}"
""".lstrip().replace(
            "python", sys.executable
        ),
        encoding="utf-8",
    )

    session = fresh_store.create_session("repo", None)
    fresh_store.set_workdir(session.id, str(workspace), managed=False)
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)

    events = FakeRunnerEvents()
    runner = AutomationRunner(events)

    await runner.send_input(session.id, "first")
    await runner.send_input(session.id, "second")
    await runner._tasks[session.id]

    assert [row["text"] for row in events.outputs] == ["first", "second"]
    assert events.awaiting_input_count == 1
    assert events.errors == []


@pytest.mark.anyio
async def test_automation_runner_does_not_strand_input_queued_during_idle_signal(
    tmp_path, fresh_store, monkeypatch
) -> None:
    """Input queued while the runner emits idle is processed by the same worker."""

    import tether.runner.automation as automation_module

    monkeypatch.setattr(automation_module, "store", fresh_store)
    monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

    workspace = tmp_path / "workspace"
    automations = workspace / ".tether" / "automations"
    automations.mkdir(parents=True)
    script = workspace / "triage.py"
    script.write_text(
        """
from __future__ import annotations

import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
Path(manifest["output_md"]).write_text(manifest["text"], encoding="utf-8")
""".lstrip(),
        encoding="utf-8",
    )
    (automations / "pokemon.yaml").write_text(
        """
name: pokemon
steps:
  - name: triage
    run:
      command:
        - python
        - triage.py
        - "{manifest}"
""".lstrip().replace(
            "python", sys.executable
        ),
        encoding="utf-8",
    )

    session = fresh_store.create_session("repo", None)
    fresh_store.set_workdir(session.id, str(workspace), managed=False)
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)

    events = FakeRunnerEvents()
    runner = AutomationRunner(events)
    queued = False

    async def queue_input() -> None:
        nonlocal queued
        if queued:
            return
        queued = True
        await runner.send_input(session.id, "second")

    events.on_awaiting_input_hook = queue_input

    await runner.send_input(session.id, "first")
    await runner._tasks[session.id]

    assert [row["text"] for row in events.outputs] == ["first", "second"]
    assert events.errors == []


@pytest.mark.anyio
async def test_automation_runner_streams_message_files(
    tmp_path, fresh_store, monkeypatch
) -> None:
    """Markdown files in output_messages_dir are emitted as bridge messages."""

    import tether.runner.automation as automation_module

    monkeypatch.setattr(automation_module, "store", fresh_store)
    monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

    workspace = tmp_path / "workspace"
    automations = workspace / ".tether" / "automations"
    automations.mkdir(parents=True)
    script = workspace / "triage.py"
    script.write_text(
        """
from __future__ import annotations

import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
messages = Path(manifest["output_messages_dir"])
messages.mkdir(parents=True, exist_ok=True)
(messages / "000-empty.md").write_text("", encoding="utf-8")
(messages / "001-start.md").write_text("Working on it", encoding="utf-8")
Path(manifest["output_md"]).write_text("Done", encoding="utf-8")
""".lstrip(),
        encoding="utf-8",
    )
    (automations / "pokemon.yaml").write_text(
        """
name: pokemon
steps:
  - name: triage
    run:
      command:
        - python
        - triage.py
        - "{manifest}"
""".lstrip().replace(
            "python", sys.executable
        ),
        encoding="utf-8",
    )

    session = fresh_store.create_session("repo", None)
    fresh_store.set_workdir(session.id, str(workspace), managed=False)
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)

    events = FakeRunnerEvents()
    runner = AutomationRunner(events)

    await runner.send_input(session.id, "check")
    await runner._tasks[session.id]

    assert [row["text"] for row in events.outputs] == ["Working on it", "Done"]
    assert events.outputs[0]["bridge_segments"] == [
        {"kind": "automation_message", "text": "Working on it"}
    ]


@pytest.mark.anyio
async def test_multiple_automations_require_name_and_strip_selector(
    tmp_path, fresh_store, monkeypatch
) -> None:
    """Multiple visible automations require an explicit selector."""

    import tether.runner.automation as automation_module

    monkeypatch.setattr(automation_module, "store", fresh_store)
    monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

    workspace = tmp_path / "workspace"
    automations = workspace / ".tether" / "automations"
    automations.mkdir(parents=True)
    (automations / "alpha.yaml").write_text(
        """
name: alpha
command:
  - echo
  - alpha
""".lstrip(),
        encoding="utf-8",
    )
    (automations / "beta.yaml").write_text(
        """
name: beta
command:
  - echo
  - beta
""".lstrip(),
        encoding="utf-8",
    )

    runner = AutomationRunner(FakeRunnerEvents())

    selected, text = runner._select_automation(str(workspace), "beta inspect this")
    prefixed, prefixed_text = runner._select_automation(
        str(workspace), "/automation:alpha inspect this"
    )
    empty_prefixed, empty_prefixed_text = runner._select_automation(
        str(workspace), "/automation:alpha"
    )

    assert selected.name == "beta"
    assert text == "inspect this"
    assert prefixed.name == "alpha"
    assert prefixed_text == "inspect this"
    assert empty_prefixed.name == "alpha"
    assert empty_prefixed_text == ""
    with pytest.raises(AutomationError, match="Choose an automation"):
        runner._select_automation(str(workspace), "inspect this")


@pytest.mark.anyio
async def test_automation_runner_reports_missing_automation(
    tmp_path, fresh_store, monkeypatch
) -> None:
    """A session without automation YAML reports a clear runner error."""

    import tether.runner.automation as automation_module

    monkeypatch.setattr(automation_module, "store", fresh_store)
    monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = fresh_store.create_session("repo", None)
    fresh_store.set_workdir(session.id, str(workspace), managed=False)
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)

    events = FakeRunnerEvents()
    runner = AutomationRunner(events)

    await runner.send_input(session.id, "hello")
    await runner._tasks[session.id]

    assert events.outputs == []
    assert events.errors[0]["code"] == "AUTOMATION_ERROR"
    assert "No automations found" in events.errors[0]["message"]


@pytest.mark.anyio
async def test_automation_runner_reports_step_failure(
    tmp_path, fresh_store, monkeypatch
) -> None:
    """A non-zero step exit is surfaced as an automation error."""

    import tether.runner.automation as automation_module

    monkeypatch.setattr(automation_module, "store", fresh_store)
    monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

    workspace = tmp_path / "workspace"
    automations = workspace / ".tether" / "automations"
    automations.mkdir(parents=True)
    script = workspace / "fail.py"
    script.write_text(
        """
from __future__ import annotations

import sys

print("bad input", file=sys.stderr)
raise SystemExit(7)
""".lstrip(),
        encoding="utf-8",
    )
    (automations / "fail.yaml").write_text(
        f"""
name: fail
steps:
  - name: reject
    run:
      command:
        - {sys.executable}
        - fail.py
""".lstrip(),
        encoding="utf-8",
    )

    session = fresh_store.create_session("repo", None)
    fresh_store.set_workdir(session.id, str(workspace), managed=False)
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)

    events = FakeRunnerEvents()
    runner = AutomationRunner(events)

    await runner.send_input(session.id, "hello")
    await runner._tasks[session.id]

    assert events.outputs == []
    assert events.errors[0]["code"] == "AUTOMATION_ERROR"
    assert "Automation step reject failed: bad input" in events.errors[0]["message"]


@pytest.mark.anyio
async def test_automation_runner_reports_timeout(
    tmp_path, fresh_store, monkeypatch
) -> None:
    """A step that exceeds its timeout is killed and reported."""

    import tether.runner.automation as automation_module

    monkeypatch.setattr(automation_module, "store", fresh_store)
    monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

    workspace = tmp_path / "workspace"
    automations = workspace / ".tether" / "automations"
    automations.mkdir(parents=True)
    script = workspace / "sleep.py"
    script.write_text(
        """
from __future__ import annotations

import time

time.sleep(2)
""".lstrip(),
        encoding="utf-8",
    )
    (automations / "slow.yaml").write_text(
        f"""
name: slow
timeout_seconds: 1
command:
  - {sys.executable}
  - sleep.py
""".lstrip(),
        encoding="utf-8",
    )

    session = fresh_store.create_session("repo", None)
    fresh_store.set_workdir(session.id, str(workspace), managed=False)
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)

    events = FakeRunnerEvents()
    runner = AutomationRunner(events)

    await runner.send_input(session.id, "hello")
    await runner._tasks[session.id]

    assert events.outputs == []
    assert events.errors[0]["code"] == "AUTOMATION_ERROR"
    assert "Automation timed out after 1 seconds" in events.errors[0]["message"]


@pytest.mark.anyio
async def test_automation_runner_uses_stdout_when_output_markdown_is_absent(
    tmp_path, fresh_store, monkeypatch
) -> None:
    """Stdout from the last step is the fallback final answer."""

    import tether.runner.automation as automation_module

    monkeypatch.setattr(automation_module, "store", fresh_store)
    monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

    workspace = tmp_path / "workspace"
    automations = workspace / ".tether" / "automations"
    automations.mkdir(parents=True)
    (automations / "stdout.yaml").write_text(
        f"""
name: stdout
command:
  - {sys.executable}
  - -c
  - print('stdout result')
""".lstrip(),
        encoding="utf-8",
    )

    session = fresh_store.create_session("repo", None)
    fresh_store.set_workdir(session.id, str(workspace), managed=False)
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)

    events = FakeRunnerEvents()
    runner = AutomationRunner(events)

    await runner.send_input(session.id, "hello")
    await runner._tasks[session.id]

    assert events.outputs[0]["text"] == "stdout result"
    assert events.errors == []


@pytest.mark.anyio
async def test_automation_runner_uses_completion_message_when_no_output(
    tmp_path, fresh_store, monkeypatch
) -> None:
    """A successful silent automation still reports where it ran."""

    import tether.runner.automation as automation_module

    monkeypatch.setattr(automation_module, "store", fresh_store)
    monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

    workspace = tmp_path / "workspace"
    automations = workspace / ".tether" / "automations"
    automations.mkdir(parents=True)
    (automations / "silent.yaml").write_text(
        f"""
name: silent
command:
  - {sys.executable}
  - -c
  - pass
""".lstrip(),
        encoding="utf-8",
    )

    session = fresh_store.create_session("repo", None)
    fresh_store.set_workdir(session.id, str(workspace), managed=False)
    session.state = SessionState.RUNNING
    fresh_store.update_session(session)

    events = FakeRunnerEvents()
    runner = AutomationRunner(events)

    await runner.send_input(session.id, "hello")
    await runner._tasks[session.id]

    assert events.outputs[0]["text"].startswith("Automation `silent` completed.")
    assert "automation-runs" in events.outputs[0]["text"]
    assert events.errors == []


@pytest.mark.anyio
async def test_start_with_missing_session_exits_cleanly() -> None:
    """A deleted session does not produce output or errors."""

    events = FakeRunnerEvents()
    runner = AutomationRunner(events)

    await runner.start("missing", "hello", approval_choice=0)
    await runner._tasks["missing"]

    assert events.outputs == []
    assert events.errors == []
    assert events.awaiting_input_count == 1


@pytest.mark.anyio
async def test_stop_cancels_active_worker_and_clears_queue() -> None:
    """Stopping an active automation cancels the worker and drops pending turns."""

    runner = AutomationRunner(FakeRunnerEvents())
    task = asyncio.create_task(asyncio.sleep(10))
    queue: asyncio.Queue[AutomationTurn] = asyncio.Queue()
    queue.put_nowait(AutomationTurn(text="queued", images=[]))
    runner._tasks["sess"] = task
    runner._queues["sess"] = queue

    result = await runner.stop("sess")

    assert result == 130
    assert queue.empty()
    assert "sess" not in runner._tasks
    assert "sess" not in runner._queues
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await runner.stop("sess") is None


@pytest.mark.anyio
async def test_automation_runner_reports_cancellation(monkeypatch) -> None:
    """Cancelling the worker emits an explicit cancellation error."""

    events = FakeRunnerEvents()
    runner = AutomationRunner(events)
    queue: asyncio.Queue[AutomationTurn] = asyncio.Queue()
    queue.put_nowait(AutomationTurn(text="queued", images=[]))
    runner._queues["sess"] = queue

    async def slow_run_turn(session_id, text, images):
        """Keep the queue worker busy until the test cancels it."""

        await asyncio.sleep(10)

    monkeypatch.setattr(runner, "_run_turn", slow_run_turn)
    task = asyncio.create_task(runner._run_queue("sess"))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events.errors == [
        {
            "session_id": "sess",
            "code": "AUTOMATION_CANCELLED",
            "message": "Automation was cancelled.",
        }
    ]


def test_write_images_rejects_mismatched_mime_type(tmp_path) -> None:
    """Image bytes must match the declared MIME type."""

    runner = AutomationRunner(FakeRunnerEvents())

    with pytest.raises(AutomationError, match="Image payload failed validation"):
        runner._write_images(
            tmp_path,
            [
                {
                    "data": base64.b64encode(PNG_BYTES).decode("ascii"),
                    "mimeType": "image/jpeg",
                }
            ],
        )


def test_resolve_cwd_uses_explicit_directory_and_rejects_missing(tmp_path) -> None:
    """Explicit cwd is resolved and missing directories fail fast."""

    runner = AutomationRunner(FakeRunnerEvents())

    assert runner._resolve_cwd(str(tmp_path), None) == tmp_path.resolve()
    assert runner._resolve_cwd(None, None).is_dir()
    with pytest.raises(AutomationError, match="Automation cwd does not exist"):
        runner._resolve_cwd(str(tmp_path / "missing"), None)


def test_load_automation_rejects_invalid_schema(tmp_path) -> None:
    """Invalid automation YAML fails with actionable errors."""

    cases = [
        ("non-mapping.yaml", "[]\n", "must contain a mapping"),
        (
            "empty-name.yaml",
            """
name: "  "
command:
  - echo
""".lstrip(),
            "has no name",
        ),
        (
            "bad-timeout.yaml",
            """
name: bad-timeout
timeout_seconds: nope
command:
  - echo
""".lstrip(),
            "timeout_seconds must be an integer",
        ),
        (
            "too-long-timeout.yaml",
            """
name: too-long-timeout
timeout_seconds: 901
command:
  - echo
""".lstrip(),
            "timeout_seconds must be between 1 and 900",
        ),
        (
            "no-steps.yaml",
            """
name: no-steps
""".lstrip(),
            "must define at least one step",
        ),
        (
            "non-mapping-step.yaml",
            """
name: non-mapping-step
steps:
  - nope
""".lstrip(),
            "step 1 must be a mapping",
        ),
        (
            "bad-command.yaml",
            """
name: bad
command: python script.py
""".lstrip(),
            "command must be a non-empty string list",
        ),
        (
            "shell.yaml",
            """
name: shell
steps:
  - name: unsafe
    run:
      shell: true
      command:
        - echo
        - hello
""".lstrip(),
            "shell=true",
        ),
    ]

    with pytest.raises(AutomationError, match="Cannot read automation"):
        load_automation(tmp_path / "missing.yaml")
    for filename, content, message in cases:
        path = tmp_path / filename
        path.write_text(content, encoding="utf-8")
        with pytest.raises(AutomationError, match=message):
            load_automation(path)


def test_project_automation_overrides_global_automation(tmp_path, monkeypatch) -> None:
    """Project automations override global automations with the same name."""

    home = tmp_path / "home"
    global_dir = home / ".config" / "tether" / "automations"
    project_dir = tmp_path / "project" / ".tether" / "automations"
    global_dir.mkdir(parents=True)
    project_dir.mkdir(parents=True)
    monkeypatch.setattr("tether.automations.Path.home", lambda: home)

    (global_dir / "shared.yaml").write_text(
        """
name: shared
command:
  - global
""".lstrip(),
        encoding="utf-8",
    )
    (project_dir / "shared.yaml").write_text(
        """
name: shared
command:
  - project
""".lstrip(),
        encoding="utf-8",
    )

    automations = load_automations(str(tmp_path / "project"))

    assert automations["shared"].steps[0].command == ("project",)


def test_images_from_payload_preserves_safe_filename() -> None:
    """Image payload normalization keeps a sanitized filename."""

    rows = images_from_payload(
        [
            {
                "type": "image",
                "data": base64.b64encode(PNG_BYTES).decode("ascii"),
                "mimeType": "image/png",
                "filename": "../card.png",
            }
        ]
    )

    assert rows[0]["filename"] == "card.png"


def test_render_template_rejects_unknown_fields() -> None:
    """Automation templates only accept known fields."""

    with pytest.raises(Exception, match="Unknown automation template field"):
        render_template("{manifest} {bad}", {"manifest": "m.json"})
