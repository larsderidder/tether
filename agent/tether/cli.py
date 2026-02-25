"""CLI entry point for Tether.

Provides ``tether start``, ``tether init``, and client subcommands
(``status``, ``list``, ``attach``, ``input``, ``interrupt``, ``sync``).

Import ordering is critical: ``load_config()`` must run before importing
``tether.main`` because that module calls ``configure_logging()`` at
module-level import time.
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> None:
    """Main CLI entry point (``tether`` command)."""
    parser = argparse.ArgumentParser(
        prog="tether",
        description="Tether \u2014 control plane for AI coding agents",
    )
    sub = parser.add_subparsers(dest="command")

    # tether start
    start_parser = sub.add_parser("start", help="Start the Tether server")
    start_parser.add_argument("--host", help="Host to bind to")
    start_parser.add_argument("--port", type=int, help="Port to bind to")
    start_parser.add_argument(
        "--dev", action="store_true", help="Enable dev mode (no auth required)"
    )

    # tether init
    sub.add_parser("init", help="Interactive setup wizard")

    # tether status
    sub.add_parser("status", help="Server health and session summary")

    # tether open
    sub.add_parser("open", help="Open the web UI in the default browser")

    # tether list
    list_parser = sub.add_parser("list", help="List sessions")
    list_parser.add_argument(
        "--external",
        action="store_true",
        help="List discoverable external sessions instead of Tether sessions",
    )
    list_parser.add_argument(
        "--directory", "-d", help="Filter sessions by directory"
    )
    list_parser.add_argument(
        "--runner-type",
        "-r",
        help="Filter external sessions by runner type (claude, codex, pi)",
    )
    list_parser.add_argument(
        "--state",
        "-s",
        help="Filter Tether sessions by state (running, awaiting_input, error, created)",
    )

    # tether attach
    attach_parser = sub.add_parser(
        "attach", help="Attach an external session to Tether"
    )
    attach_parser.add_argument(
        "external_id",
        nargs="?",
        help="External session ID (prefix is fine; omit to pick from current directory)",
    )
    attach_parser.add_argument(
        "--runner-type",
        "-r",
        default="claude_code",
        help="Runner type (default: claude_code)",
    )
    attach_parser.add_argument(
        "--directory",
        "-d",
        default=".",
        help="Working directory (default: current directory)",
    )
    attach_parser.add_argument(
        "--platform",
        "-p",
        help="Bind to a messaging platform (telegram, slack, discord)",
    )

    # tether input
    input_parser = sub.add_parser("input", help="Send input to a session")
    input_parser.add_argument("session_id", help="Session ID (prefix is fine)")
    input_parser.add_argument("text", help="Text to send")

    # tether interrupt
    interrupt_parser = sub.add_parser(
        "interrupt", help="Interrupt a running session"
    )
    interrupt_parser.add_argument(
        "session_id", help="Session ID (prefix is fine)"
    )

    # tether delete
    delete_parser = sub.add_parser("delete", help="Delete a session")
    delete_parser.add_argument(
        "session_id", help="Session ID (prefix is fine)"
    )

    # tether sync
    sync_parser = sub.add_parser(
        "sync",
        help="Pull new messages from an attached external session",
    )
    sync_parser.add_argument("session_id", help="Session ID (prefix is fine)")

    args = parser.parse_args(argv)

    if args.command == "start":
        _run_start(args)
    elif args.command == "init":
        _run_init()
    elif args.command in (
        "status", "open", "list", "attach", "input", "interrupt", "delete", "sync",
    ):
        _run_client(args)
    else:
        parser.print_help()
        sys.exit(1)


def _run_start(args: argparse.Namespace) -> None:
    """Handle ``tether start``."""
    # Apply CLI flag overrides BEFORE loading config
    if args.host:
        os.environ["TETHER_AGENT_HOST"] = args.host
    if args.port:
        os.environ["TETHER_AGENT_PORT"] = str(args.port)
    if args.dev:
        os.environ["TETHER_AGENT_DEV_MODE"] = "1"

    # Load config from .env files (must happen before importing main)
    from tether.config import load_config

    load_config()

    from tether.main import run

    run()


def _run_init() -> None:
    """Handle ``tether init``."""
    from tether.init_wizard import run_wizard

    run_wizard()


def _run_client(args: argparse.Namespace) -> None:
    """Handle client subcommands that talk to a running server."""
    # Load config so we pick up token, host, port from .env files
    from tether.config import load_config

    load_config()

    from tether.cli_client import (
        cmd_attach,
        cmd_delete,
        cmd_input,
        cmd_interrupt,
        cmd_list,
        cmd_list_external,
        cmd_open,
        cmd_status,
        cmd_sync,
    )

    if args.command == "status":
        cmd_status()
    elif args.command == "open":
        cmd_open()
    elif args.command == "list":
        if args.external:
            cmd_list_external(args.directory, args.runner_type)
        else:
            cmd_list(state=args.state, directory=args.directory)
    elif args.command == "attach":
        directory = os.path.abspath(args.directory)
        cmd_attach(args.external_id, args.runner_type, directory, args.platform)
    elif args.command == "input":
        cmd_input(args.session_id, args.text)
    elif args.command == "interrupt":
        cmd_interrupt(args.session_id)
    elif args.command == "delete":
        cmd_delete(args.session_id)
    elif args.command == "sync":
        cmd_sync(args.session_id)


if __name__ == "__main__":
    main()
