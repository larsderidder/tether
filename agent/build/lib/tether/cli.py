"""CLI entry point for Tether.

Provides ``tether start`` and ``tether init`` subcommands.

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
        description="Tether — control plane for AI coding agents",
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

    args = parser.parse_args(argv)

    if args.command == "start":
        _run_start(args)
    elif args.command == "init":
        _run_init()
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


if __name__ == "__main__":
    main()
