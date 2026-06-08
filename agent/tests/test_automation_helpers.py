"""Tests for Python helpers used by automation scripts."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from tether.automation_helpers import (
    AutomationPiError,
    _images_from_manifest,
    _read_pi_answer,
    _stop_pi_process,
    ask_pi,
    ask_pi_from_manifest,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 16)


class FakeStdin:
    """Writable stdin stub that records JSON-line commands."""

    def __init__(self) -> None:
        """Create an empty command sink."""

        self.lines: list[str] = []

    def write(self, data: bytes) -> None:
        """Record one write call."""

        self.lines.append(data.decode())

    async def drain(self) -> None:
        """Match asyncio stream writer shape."""


class FakeStream:
    """Readable stream stub for stdout and stderr."""

    def __init__(self, lines: list[bytes] | None = None, data: bytes = b"") -> None:
        """Create a stream with queued lines and optional full data."""

        self._lines = list(lines or [])
        self._data = data

    async def readline(self) -> bytes:
        """Return the next queued line."""

        if self._lines:
            return self._lines.pop(0)
        return b""

    async def read(self) -> bytes:
        """Return full stream data."""

        return self._data


class FakeProcess:
    """Small subprocess stand-in for pi helper tests."""

    def __init__(
        self, stdout_lines: list[dict[str, Any] | bytes], stderr: bytes = b""
    ) -> None:
        """Create a process with serialized stdout events."""

        self.stdin = FakeStdin()
        self.stdout = FakeStream([_encode_line(line) for line in stdout_lines])
        self.stderr = FakeStream(data=stderr)
        self.returncode: int | None = None
        self.killed = False

    async def wait(self) -> int:
        """Mark the fake process as stopped."""

        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def kill(self) -> None:
        """Mark the fake process as killed."""

        self.killed = True
        self.returncode = -9


def _encode_line(line: dict[str, Any] | bytes) -> bytes:
    """Encode one fake stdout line."""

    if isinstance(line, bytes):
        return line
    return json.dumps(line).encode() + b"\n"


def _agent_end(text: str) -> dict[str, Any]:
    """Build a pi agent_end event with final assistant text."""

    return {
        "type": "agent_end",
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def _patch_pi_process(
    monkeypatch, process: FakeProcess, calls: list[dict[str, Any]]
) -> None:
    """Patch pi binary discovery and process spawning."""

    async def fake_spawn(*args, **kwargs):
        """Return the configured fake pi process."""

        calls.append({"args": args, "kwargs": kwargs})
        return process

    monkeypatch.setattr("tether.automation_helpers._find_pi_binary", lambda: "/bin/pi")
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)


def test_ask_pi_from_manifest_accepts_manifest_dict_and_skips_images(
    monkeypatch,
) -> None:
    """Manifest dictionaries can be used directly without image conversion."""

    process = FakeProcess([_agent_end("Dict answer")])
    calls: list[dict[str, Any]] = []
    _patch_pi_process(monkeypatch, process, calls)

    answer = ask_pi_from_manifest(
        {"text": "dict prompt", "images": ["bad row"]}, include_images=False
    )

    assert answer == "Dict answer"
    prompt_command = json.loads(process.stdin.lines[0])
    assert prompt_command["message"] == "dict prompt"
    assert prompt_command["images"] == []


def test_ask_pi_from_manifest_passes_images_and_writes_output(
    tmp_path, monkeypatch
) -> None:
    """The manifest helper sends image payloads and writes final Markdown."""

    image_path = tmp_path / "card.png"
    image_path.write_bytes(PNG_BYTES)
    output_md = tmp_path / "output.md"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "text": "fallback prompt",
                "output_md": str(output_md),
                "images": [
                    {
                        "path": str(image_path),
                        "filename": "card.png",
                        "mime_type": "image/png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    process = FakeProcess([_agent_end("Final answer")])
    calls: list[dict[str, Any]] = []
    _patch_pi_process(monkeypatch, process, calls)

    answer = ask_pi_from_manifest(manifest_path, "custom prompt")

    assert answer == "Final answer"
    assert output_md.read_text(encoding="utf-8") == "Final answer"
    prompt_command = json.loads(process.stdin.lines[0])
    assert prompt_command["type"] == "prompt"
    assert prompt_command["message"] == "custom prompt"
    assert prompt_command["images"] == [
        {
            "type": "image",
            "data": base64.b64encode(PNG_BYTES).decode("ascii"),
            "mimeType": "image/png",
            "filename": "card.png",
        }
    ]
    assert json.loads(process.stdin.lines[-1])["type"] == "abort"
    assert calls[0]["args"][:3] == ("/bin/pi", "--mode", "rpc")


@pytest.mark.anyio
async def test_ask_pi_passes_session_file_and_model(tmp_path, monkeypatch) -> None:
    """Session files are passed through with their stored pi model."""

    process = FakeProcess([_agent_end("Resumed answer")])
    calls: list[dict[str, Any]] = []
    _patch_pi_process(monkeypatch, process, calls)
    session_file = tmp_path / "session.jsonl"
    session_file.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        "tether.automation_helpers.get_pi_session_model",
        lambda path: ("openai", "gpt-4.1"),
    )

    answer = await ask_pi("hello", session_file=session_file, cwd=tmp_path)

    assert answer == "Resumed answer"
    assert calls[0]["args"] == (
        "/bin/pi",
        "--mode",
        "rpc",
        "--session",
        str(session_file),
        "--model",
        "openai/gpt-4.1",
    )
    assert calls[0]["kwargs"]["cwd"] == str(tmp_path)


@pytest.mark.anyio
async def test_ask_pi_raises_for_prompt_response_error(monkeypatch) -> None:
    """Failed pi prompt responses become Python exceptions."""

    process = FakeProcess(
        [
            {
                "type": "response",
                "command": "prompt",
                "success": False,
                "error": "bad key",
            }
        ]
    )
    calls: list[dict[str, Any]] = []
    _patch_pi_process(monkeypatch, process, calls)

    with pytest.raises(AutomationPiError, match="pi prompt failed: bad key"):
        await ask_pi("hello")


@pytest.mark.anyio
async def test_ask_pi_times_out_and_kills_process(monkeypatch) -> None:
    """Timeouts stop the pi process instead of leaving it running."""

    process = FakeProcess([])
    calls: list[dict[str, Any]] = []
    _patch_pi_process(monkeypatch, process, calls)

    with pytest.raises(AutomationPiError, match="pi prompt timed out after 0 seconds"):
        await ask_pi("hello", timeout_seconds=0)

    assert process.returncode == 0 or process.killed


@pytest.mark.anyio
async def test_ask_pi_reports_pi_stream_errors(monkeypatch) -> None:
    """Stream errors are raised with useful messages."""

    process = FakeProcess(
        [
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "error", "reason": "nope"},
            }
        ]
    )
    calls: list[dict[str, Any]] = []
    _patch_pi_process(monkeypatch, process, calls)

    with pytest.raises(AutomationPiError, match="pi stream error: nope"):
        await ask_pi("hello")


@pytest.mark.anyio
async def test_ask_pi_reports_retry_failure(monkeypatch) -> None:
    """Pi retry exhaustion is raised as an automation helper error."""

    process = FakeProcess(
        [{"type": "auto_retry_end", "success": False, "finalError": "timeout"}]
    )
    calls: list[dict[str, Any]] = []
    _patch_pi_process(monkeypatch, process, calls)

    with pytest.raises(AutomationPiError, match="pi retry failed: timeout"):
        await ask_pi("hello")


@pytest.mark.anyio
async def test_ask_pi_reports_empty_final_answer(monkeypatch) -> None:
    """An agent_end event without assistant text is treated as failure."""

    process = FakeProcess([_agent_end("")])
    calls: list[dict[str, Any]] = []
    _patch_pi_process(monkeypatch, process, calls)

    with pytest.raises(AutomationPiError, match="finished without assistant text"):
        await ask_pi("hello")


@pytest.mark.anyio
async def test_ask_pi_reports_eof_with_stderr(monkeypatch) -> None:
    """EOF before agent_end includes stderr when pi wrote any."""

    process = FakeProcess([], stderr=b"crashed")
    calls: list[dict[str, Any]] = []
    _patch_pi_process(monkeypatch, process, calls)

    with pytest.raises(AutomationPiError, match="before returning an answer: crashed"):
        await ask_pi("hello")


@pytest.mark.anyio
async def test_ask_pi_reports_missing_binary(monkeypatch) -> None:
    """The helper fails clearly when pi is not installed for Tether."""

    monkeypatch.setattr("tether.automation_helpers._find_pi_binary", lambda: None)

    with pytest.raises(AutomationPiError, match="pi binary not found"):
        await ask_pi("hello")


def test_images_from_manifest_skips_invalid_rows(tmp_path) -> None:
    """Only readable image rows with MIME types become pi payloads."""

    image_path = tmp_path / "card.png"
    image_path.write_bytes(PNG_BYTES)

    payloads = _images_from_manifest(
        {
            "images": [
                "not a row",
                {"path": str(tmp_path / "missing.png"), "mime_type": "image/png"},
                {"path": str(image_path)},
                {"path": str(image_path), "mimeType": "image/png"},
            ]
        }
    )

    assert len(payloads) == 1
    assert payloads[0]["filename"] == "card.png"


@pytest.mark.anyio
async def test_read_pi_answer_rejects_missing_stdout() -> None:
    """A malformed process object fails clearly."""

    process = FakeProcess([])
    process.stdout = None

    with pytest.raises(AutomationPiError, match="pi stdout is not available"):
        await _read_pi_answer(process, timeout_seconds=1)


@pytest.mark.anyio
async def test_stop_pi_process_ignores_already_stopped_process() -> None:
    """The stop helper leaves completed processes alone."""

    process = FakeProcess([])
    process.returncode = 0

    await _stop_pi_process(process)

    assert process.stdin.lines == []


@pytest.mark.anyio
async def test_ask_pi_ignores_notification_prefix(monkeypatch) -> None:
    """Pi terminal notification prefixes do not hide JSON events."""

    raw = (
        b"]777;notify;pi;Ready!"
        b'{"type":"agent_end","messages":[{"role":"assistant",'
        b'"content":[{"type":"text","text":"Ready"}]}]}\n'
    )
    process = FakeProcess([raw])
    calls: list[dict[str, Any]] = []
    _patch_pi_process(monkeypatch, process, calls)

    answer = await ask_pi("hello")

    assert answer == "Ready"
