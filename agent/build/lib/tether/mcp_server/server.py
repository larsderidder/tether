"""MCP server entry point.

This module provides the main() function that starts an MCP server
using stdio transport, making Tether tools available to MCP clients.
"""

import os
import sys
import traceback
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp import types
from mcp.server import Server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@asynccontextmanager
async def _stdio_server():
    """Custom stdio server wrapper that uses sys.stdin/stdout directly."""
    debug_io = os.environ.get("TETHER_MCP_DEBUG_IO") == "1"
    if debug_io:
        _stderr(
            "[mcp-stdio] stdin"
            f" closed={sys.stdin.closed}"
            f" isatty={sys.stdin.isatty()}"
            f" fileno={getattr(sys.stdin, 'fileno', lambda: 'n/a')()}"
        )

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    async def stdin_reader() -> None:
        try:
            async with read_stream_writer:
                while True:
                    line = await anyio.to_thread.run_sync(sys.stdin.readline)
                    if not line:
                        break
                    if debug_io:
                        _stderr(f"[mcp-stdio] recv: {line.rstrip()}")
                    try:
                        message = JSONRPCMessage.model_validate_json(line)
                    except Exception as exc:
                        await read_stream_writer.send(exc)
                        continue
                    await read_stream_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async def stdout_writer() -> None:
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    json = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
                    await anyio.to_thread.run_sync(sys.stdout.write, json + "\n")
                    await anyio.to_thread.run_sync(sys.stdout.flush)
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as tg:
        tg.start_soon(stdin_reader)
        tg.start_soon(stdout_writer)
        yield read_stream, write_stream


def main() -> None:
    """Main entry point for MCP server.

    Starts an MCP server using stdio transport, exposing Tether
    functionality as MCP tools.
    """
    _stderr("Starting MCP server")

    # Load config from env files so the MCP server can find the token/port
    from tether.config import load_config

    load_config()

    try:
        import mcp  # noqa: F401
    except ImportError:
        print("ERROR: MCP SDK not installed. Install with: pip install mcp", file=sys.stderr)
        sys.exit(1)

    # Import tool functions
    from tether.mcp_server.tools import execute_tool, get_tool_definitions

    # Create MCP server (low-level)
    server = Server("tether-agent")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        tools: list[types.Tool] = []
        for tool in get_tool_definitions():
            tools.append(
                types.Tool(
                    name=tool["name"],
                    description=tool.get("description"),
                    inputSchema=tool["input_schema"],
                )
            )
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> Any:
        return await execute_tool(name, arguments or {})

    _stderr("Registered MCP tools: 4")

    async def _run_stdio() -> None:
        async with _stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    anyio.run(_run_stdio)


if __name__ == "__main__":
    main()
