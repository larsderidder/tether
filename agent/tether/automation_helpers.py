"""Helpers for scripts launched by Tether automations."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

from tether.discovery.pi_sessions import get_pi_session_model
from tether.runner.pi_rpc import _PI_RPC_STREAM_LIMIT_BYTES, _find_pi_binary


class AutomationPiError(RuntimeError):
    """Raised when the pi helper cannot complete a prompt."""


async def ask_pi(
    prompt: str,
    *,
    images: list[dict[str, str]] | None = None,
    cwd: str | Path | None = None,
    output_markdown: str | Path | None = None,
    session_file: str | Path | None = None,
    timeout_seconds: int = 900,
) -> str:
    """Ask pi through its RPC mode and return the final assistant text."""

    pi_bin = _find_pi_binary()
    if not pi_bin:
        raise AutomationPiError(
            "pi binary not found. Install pi or make sure Tether's PATH can find it."
        )

    args = [pi_bin, "--mode", "rpc"]
    if session_file:
        session_path = Path(session_file).expanduser()
        args.extend(["--session", str(session_path)])
        session_model = get_pi_session_model(session_path)
        if session_model:
            provider, model_id = session_model
            args.extend(["--model", f"{provider}/{model_id}"])

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(Path(cwd).expanduser()) if cwd else None,
        limit=_PI_RPC_STREAM_LIMIT_BYTES,
    )
    try:
        await _write_rpc_command(
            proc,
            {
                "type": "prompt",
                "message": prompt,
                "images": images or [],
            },
        )
        answer = await _read_pi_answer(proc, timeout_seconds=timeout_seconds)
        if output_markdown:
            Path(output_markdown).expanduser().write_text(answer, encoding="utf-8")
        return answer
    finally:
        await _stop_pi_process(proc)


def ask_pi_from_manifest(
    manifest: str | Path | dict[str, Any],
    prompt: str | None = None,
    *,
    include_images: bool = True,
    output_markdown: str | Path | None = None,
    cwd: str | Path | None = None,
    session_file: str | Path | None = None,
    timeout_seconds: int = 900,
) -> str:
    """Ask pi using paths and images from an automation manifest."""

    data = _load_manifest(manifest)
    message = prompt if prompt is not None else str(data.get("text") or "")
    images = _images_from_manifest(data) if include_images else []
    markdown_path = output_markdown or data.get("output_md")
    return asyncio.run(
        ask_pi(
            message,
            images=images,
            cwd=cwd,
            output_markdown=markdown_path,
            session_file=session_file,
            timeout_seconds=timeout_seconds,
        )
    )


def _load_manifest(manifest: str | Path | dict[str, Any]) -> dict[str, Any]:
    """Load a manifest from a path or return a copy of a mapping."""

    if isinstance(manifest, dict):
        return dict(manifest)
    path = Path(manifest).expanduser()
    return json.loads(path.read_text(encoding="utf-8"))


def _images_from_manifest(manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Convert manifest image rows into pi RPC image payloads."""

    payloads: list[dict[str, str]] = []
    for row in manifest.get("images") or []:
        if not isinstance(row, dict):
            continue
        path = Path(str(row.get("path") or "")).expanduser()
        mime_type = str(row.get("mime_type") or row.get("mimeType") or "").strip()
        if not path.is_file() or not mime_type:
            continue
        payloads.append(
            {
                "type": "image",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                "mimeType": mime_type,
                "filename": str(row.get("filename") or path.name),
            }
        )
    return payloads


async def _write_rpc_command(
    proc: asyncio.subprocess.Process, command: dict[str, Any]
) -> None:
    """Write one JSON-line command to pi."""

    if proc.stdin is None:
        raise AutomationPiError("pi stdin is not available")
    line = json.dumps(command, separators=(",", ":")) + "\n"
    proc.stdin.write(line.encode())
    await proc.stdin.drain()


async def _read_pi_answer(
    proc: asyncio.subprocess.Process,
    *,
    timeout_seconds: int,
) -> str:
    """Read pi events until the prompt finishes."""

    if proc.stdout is None:
        raise AutomationPiError("pi stdout is not available")

    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AutomationPiError(
                f"pi prompt timed out after {timeout_seconds} seconds"
            )
        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        if not raw:
            stderr = await _read_stderr(proc)
            detail = f": {stderr}" if stderr else ""
            raise AutomationPiError(f"pi exited before returning an answer{detail}")

        event = _parse_pi_event(raw)
        if not event:
            continue

        event_type = event.get("type")
        if event_type == "response":
            _raise_for_failed_response(event)
        elif event_type == "agent_end":
            answer = _assistant_text_from_agent_end(event)
            if answer:
                return answer
            raise AutomationPiError("pi finished without assistant text")
        elif event_type == "auto_retry_end" and event.get("success") is False:
            raise AutomationPiError(
                f"pi retry failed: {event.get('finalError') or 'unknown error'}"
            )
        elif event_type == "message_update":
            delta = event.get("assistantMessageEvent", {})
            if isinstance(delta, dict) and delta.get("type") == "error":
                raise AutomationPiError(
                    f"pi stream error: {delta.get('reason') or 'unknown'}"
                )


def _parse_pi_event(raw: bytes | str) -> dict[str, Any] | None:
    """Parse one pi JSON event, ignoring notification prefixes and plain text."""

    line = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    if "]777;notify;" in line:
        json_start = line.find('{"type":')
        if json_start > 0:
            line = line[json_start:]
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _raise_for_failed_response(event: dict[str, Any]) -> None:
    """Raise for failed pi command responses."""

    if event.get("success", False):
        return
    command = event.get("command") or "command"
    error = event.get("error") or "Unknown error"
    raise AutomationPiError(f"pi {command} failed: {error}")


def _assistant_text_from_agent_end(event: dict[str, Any]) -> str:
    """Extract the final assistant text from an agent_end event."""

    parts: list[str] = []
    messages = event.get("messages") or []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "")
                if text:
                    parts.append(text)
    return "\n".join(parts).strip()


async def _read_stderr(proc: asyncio.subprocess.Process) -> str:
    """Read any available pi stderr text."""

    if proc.stderr is None:
        return ""
    try:
        data = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
    except (asyncio.TimeoutError, Exception):
        return ""
    return data.decode(errors="replace").strip()


async def _stop_pi_process(proc: asyncio.subprocess.Process) -> None:
    """Ask pi to stop, then kill it if it stays alive."""

    if proc.returncode is not None:
        return
    try:
        await _write_rpc_command(proc, {"type": "abort"})
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except Exception:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except Exception:
            pass
