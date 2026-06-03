"""Tests for the runbook runner adapter."""

from __future__ import annotations

import base64
import sys

import pytest

from tether.bridges.image_io import images_from_payload
from tether.models import SessionState
from tether.runner.runbook import RunbookRunner, render_template

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 16)


class FakeRunnerEvents:
    """Fake event sink for runbook runner tests."""

    def __init__(self) -> None:
        """Create an empty event sink."""

        self.outputs: list[dict] = []
        self.errors: list[dict] = []
        self.awaiting_input_count = 0

    async def on_output(self, session_id, stream, text, *, kind="final", is_final=None):
        """Record runner output."""

        self.outputs.append(
            {
                "session_id": session_id,
                "stream": stream,
                "text": text,
                "kind": kind,
                "is_final": is_final,
            }
        )

    async def on_error(self, session_id, code, message):
        """Record runner errors."""

        self.errors.append({"session_id": session_id, "code": code, "message": message})

    async def on_awaiting_input(self, session_id):
        """Record idle transitions."""

        self.awaiting_input_count += 1


@pytest.mark.anyio
async def test_runbook_runner_saves_images_and_returns_markdown(
    tmp_path, fresh_store, monkeypatch
) -> None:
    """A runbook turn writes a manifest, image files, and returns output.md."""

    import tether.runner.runbook as runbook_module

    monkeypatch.setattr(runbook_module, "store", fresh_store)
    monkeypatch.setenv("TETHER_AGENT_DATA_DIR", str(tmp_path / "data"))

    workspace = tmp_path / "workspace"
    runbooks = workspace / ".tether" / "runbooks"
    runbooks.mkdir(parents=True)
    script = workspace / "triage.py"
    script.write_text(
        """
from __future__ import annotations

import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
Path(manifest["output_md"]).write_text(
    f"# Done\\nImages: {len(manifest['images'])}\\nText: {manifest['text']}\\n",
    encoding="utf-8",
)
""".lstrip(),
        encoding="utf-8",
    )
    (runbooks / "pokemon.yaml").write_text(
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
    runner = RunbookRunner(events)
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
    assert events.outputs[0]["text"] == "# Done\nImages: 1\nText: check this"
    image_files = list(
        (tmp_path / "data" / "runbook-runs" / session.id).glob("pokemon-*/input/*.png")
    )
    assert len(image_files) == 1
    assert image_files[0].read_bytes() == PNG_BYTES


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
    """Runbook templates only accept known fields."""

    with pytest.raises(Exception, match="Unknown runbook template field"):
        render_template("{manifest} {bad}", {"manifest": "m.json"})
