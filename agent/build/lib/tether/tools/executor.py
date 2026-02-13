"""Tool execution handlers for Claude runner."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog

from tether.store import store

logger = structlog.get_logger(__name__)


async def execute_tool(session_id: str, tool_name: str, tool_input: dict) -> dict:
    """Execute a tool and return the result.

    Args:
        session_id: Internal session identifier for workdir resolution.
        tool_name: Name of the tool to execute.
        tool_input: Tool input parameters.

    Returns:
        Dict with 'success' bool and either 'result' or 'error'.
    """
    try:
        if tool_name == "file_read":
            result = await _execute_file_read(session_id, tool_input)
        elif tool_name == "file_write":
            result = await _execute_file_write(session_id, tool_input)
        elif tool_name == "bash":
            result = await _execute_bash(session_id, tool_input)
        else:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        return {"success": True, "result": result}
    except Exception as e:
        logger.exception("Tool execution failed", tool=tool_name, session_id=session_id)
        return {"success": False, "error": str(e)}


def _resolve_path(session_id: str, path: str) -> str:
    """Resolve a path relative to the session working directory.

    Args:
        session_id: Internal session identifier.
        path: Relative or absolute path.

    Returns:
        Absolute path within the working directory.

    Raises:
        ValueError: If path escapes the working directory.
    """
    workdir = store.get_workdir(session_id)
    if not workdir:
        raise ValueError("No working directory set for session")

    if os.path.isabs(path):
        resolved = os.path.normpath(path)
    else:
        resolved = os.path.normpath(os.path.join(workdir, path))

    # Prevent directory traversal
    if not resolved.startswith(workdir.rstrip("/") + "/") and resolved != workdir:
        raise ValueError(f"Path escapes working directory: {path}")

    return resolved


async def _execute_file_read(session_id: str, tool_input: dict) -> str:
    """Read file contents with line numbers.

    Args:
        session_id: Internal session identifier.
        tool_input: Tool input with 'path', optional 'offset' and 'limit'.

    Returns:
        File contents with line numbers.
    """
    path = tool_input.get("path")
    if not path:
        raise ValueError("Missing required parameter: path")

    offset = tool_input.get("offset", 1)
    limit = tool_input.get("limit", 2000)

    resolved = _resolve_path(session_id, path)

    if not os.path.exists(resolved):
        raise FileNotFoundError(f"File not found: {path}")

    if not os.path.isfile(resolved):
        raise ValueError(f"Not a file: {path}")

    with open(resolved, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    # Apply offset and limit (offset is 1-indexed)
    start_idx = max(0, offset - 1)
    end_idx = start_idx + limit
    selected_lines = lines[start_idx:end_idx]

    # Format with line numbers
    result_lines = []
    for i, line in enumerate(selected_lines, start=start_idx + 1):
        # Strip trailing newline for consistent formatting
        line_content = line.rstrip("\n\r")
        result_lines.append(f"{i:6}\t{line_content}")

    return "\n".join(result_lines)


async def _execute_file_write(session_id: str, tool_input: dict) -> str:
    """Write content to a file, creating parent directories as needed.

    Args:
        session_id: Internal session identifier.
        tool_input: Tool input with 'path' and 'content'.

    Returns:
        Success message.
    """
    path = tool_input.get("path")
    content = tool_input.get("content")

    if not path:
        raise ValueError("Missing required parameter: path")
    if content is None:
        raise ValueError("Missing required parameter: content")

    resolved = _resolve_path(session_id, path)

    # Create parent directories
    parent = os.path.dirname(resolved)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(resolved, "w", encoding="utf-8") as f:
        f.write(content)

    return f"Successfully wrote {len(content)} bytes to {path}"


async def _execute_bash(session_id: str, tool_input: dict) -> str:
    """Execute a bash command with timeout.

    Args:
        session_id: Internal session identifier.
        tool_input: Tool input with 'command' and optional 'timeout'.

    Returns:
        Combined stdout and stderr output.
    """
    command = tool_input.get("command")
    if not command:
        raise ValueError("Missing required parameter: command")

    timeout = tool_input.get("timeout", 120)

    workdir = store.get_workdir(session_id)
    if not workdir:
        raise ValueError("No working directory set for session")

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=workdir,
        )

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"Command timed out after {timeout}s")

        output = stdout.decode("utf-8", errors="replace") if stdout else ""

        if proc.returncode != 0:
            return f"Command exited with code {proc.returncode}\n{output}"

        return output if output else "(no output)"

    except Exception as e:
        if isinstance(e, (TimeoutError, ValueError)):
            raise
        raise RuntimeError(f"Failed to execute command: {e}")
