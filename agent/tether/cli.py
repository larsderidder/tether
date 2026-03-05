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
import subprocess
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
    list_parser.add_argument("--directory", "-d", help="Filter sessions by directory")
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

    # tether new
    new_parser = sub.add_parser("new", help="Create a new session")
    new_parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Working directory (default: current directory)",
    )
    new_parser.add_argument(
        "--adapter",
        "-a",
        help="Agent adapter (claude_auto, opencode, pi, codex, ...)",
    )
    new_parser.add_argument(
        "--prompt",
        "-m",
        help="Start the session immediately with this prompt",
    )
    new_parser.add_argument(
        "--platform",
        "-p",
        help="Bind to a messaging platform (telegram, slack, discord)",
    )

    # tether input
    input_parser = sub.add_parser("input", help="Send input to a session")
    input_parser.add_argument("session_id", help="Session ID (prefix is fine)")
    input_parser.add_argument("text", help="Text to send")

    # tether interrupt
    interrupt_parser = sub.add_parser("interrupt", help="Interrupt a running session")
    interrupt_parser.add_argument("session_id", help="Session ID (prefix is fine)")

    # tether delete
    delete_parser = sub.add_parser("delete", help="Delete a session")
    delete_parser.add_argument("session_id", help="Session ID (prefix is fine)")

    # tether sync
    sync_parser = sub.add_parser(
        "sync",
        help="Pull new messages from an attached external session",
    )
    sync_parser.add_argument("session_id", help="Session ID (prefix is fine)")

    # tether watch
    watch_parser = sub.add_parser(
        "watch", help="Stream live output from a session to the terminal"
    )
    watch_parser.add_argument("session_id", help="Session ID (prefix is fine)")

    # tether server <init|status|upgrade|logs>
    server_parser = sub.add_parser("server", help="Manage remote Tether servers")
    server_sub = server_parser.add_subparsers(dest="server_command")

    # tether server init <host>
    server_init_p = server_sub.add_parser(
        "init", help="Bootstrap a remote Tether server over SSH"
    )
    server_init_p.add_argument(
        "host", help="SSH host (name, IP, or Tailscale hostname)"
    )
    server_init_p.add_argument(
        "--name", help="Local alias for this server (default: host)"
    )
    server_init_p.add_argument("--user", "-u", help="SSH user (default: current user)")
    server_init_p.add_argument(
        "--port",
        type=int,
        default=8787,
        help="Port for Tether to listen on (default: 8787)",
    )

    # tether server status <name>
    server_status_p = server_sub.add_parser(
        "status", help="Check health of a registered remote server"
    )
    server_status_p.add_argument("name", help="Server alias from servers.yaml")

    # tether server upgrade <name>
    server_upgrade_p = server_sub.add_parser(
        "upgrade", help="Upgrade tether-ai on a remote server"
    )
    server_upgrade_p.add_argument("name", help="Server alias from servers.yaml")

    # tether server logs <name>
    server_logs_p = server_sub.add_parser(
        "logs", help="Stream systemd logs from a remote server"
    )
    server_logs_p.add_argument("name", help="Server alias from servers.yaml")
    server_logs_p.add_argument(
        "--lines",
        "-n",
        type=int,
        default=50,
        help="Number of recent lines (default: 50)",
    )
    server_logs_p.add_argument(
        "--follow",
        "-f",
        action="store_true",
        help="Follow log output (like journalctl -f)",
    )

    args = parser.parse_args(argv)

    if args.command == "start":
        _run_start(args)
    elif args.command == "init":
        _run_init()
    elif args.command == "server":
        _run_server(args)
    elif args.command in (
        "status",
        "open",
        "list",
        "attach",
        "new",
        "input",
        "interrupt",
        "delete",
        "sync",
        "watch",
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
        cmd_new,
        cmd_open,
        cmd_status,
        cmd_sync,
        cmd_watch,
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
    elif args.command == "new":
        directory = os.path.abspath(args.directory)
        cmd_new(directory, args.adapter, args.prompt, args.platform)
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
    elif args.command == "watch":
        cmd_watch(args.session_id)


def _run_server(args: argparse.Namespace) -> None:
    """Handle ``tether server`` subcommands."""
    cmd = getattr(args, "server_command", None)

    if cmd is None:
        # Ran ``tether server`` with no subcommand.
        print("Usage: tether server <init|status|upgrade|logs>")
        sys.exit(1)

    if cmd == "init":
        _run_server_init(args)
    elif cmd == "status":
        _run_server_status(args)
    elif cmd == "upgrade":
        _run_server_upgrade(args)
    elif cmd == "logs":
        _run_server_logs(args)
    else:
        print(f"Unknown server command: {cmd}")
        sys.exit(1)


def _run_server_init(args: argparse.Namespace) -> None:
    """Handle ``tether server init <host>``."""
    import getpass

    from tether.server_init import run_server_init

    host = args.host
    name = getattr(args, "name", None) or host
    user = getattr(args, "user", None)
    port = getattr(args, "port", 8787)

    # Interactive prompts
    print()
    print(f"Initialising remote Tether server on {host}")
    print()

    # Confirm name
    suggested_name = name
    try:
        entered = input(f"  Server alias [{suggested_name}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if entered:
        name = entered

    # Confirm user
    default_user = user or getpass.getuser()
    try:
        entered = input(f"  SSH user [{default_user}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if entered:
        user = entered
    elif user is None:
        user = None  # let ssh config decide

    # Confirm port
    try:
        entered = input(f"  Tether port [{port}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if entered:
        try:
            port = int(entered)
        except ValueError:
            print(f"  Invalid port number: {entered}", file=sys.stderr)
            sys.exit(1)

    # Optional Telegram bridge
    telegram_token = None
    telegram_group_id = None
    try:
        want_tg = input("  Configure Telegram bridge? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if want_tg in ("y", "yes"):
        try:
            telegram_token = input("    Telegram bot token: ").strip() or None
            telegram_group_id = input("    Telegram forum group ID: ").strip() or None
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

    print()
    print(f"  Host:    {host}")
    print(f"  Alias:   {name}")
    if user:
        print(f"  SSH user: {user}")
    print(f"  Port:    {port}")
    if telegram_token:
        print(f"  Telegram: configured")
    print()

    try:
        confirm = input("Proceed? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if confirm and confirm not in ("y", "yes"):
        print("Aborted.")
        sys.exit(0)

    print()

    try:
        result = run_server_init(
            host,
            name=name,
            user=user,
            port=port,
            telegram_token=telegram_token,
            telegram_group_id=telegram_group_id,
            log=print,
        )
    except ConnectionError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)

    print()
    print(f"Done! Remote Tether server '{result.name}' is ready.")
    print()
    print(f"  Host:  {result.host}:{result.port}")
    print(f"  Token: {result.token[:8]}... (saved to ~/.config/tether/servers.yaml)")
    print()
    print("To switch to this server:")
    print(f"  tether context use {result.name}")
    print(f"  tether status")


def _run_server_status(args: argparse.Namespace) -> None:
    """Handle ``tether server status <name>``."""
    from tether.servers import get_server
    from tether.server_init import ssh_run

    name = args.name
    profile = get_server(name)
    if profile is None:
        print(
            f"Error: unknown server '{name}'. Check ~/.config/tether/servers.yaml.",
            file=sys.stderr,
        )
        sys.exit(1)

    host = profile.get("host", name)
    port = profile.get("port", "8787")

    print(f"Checking {name} ({host}:{port})...")
    rc, out, _ = ssh_run(
        host,
        f"curl -sf http://127.0.0.1:{port}/api/health 2>/dev/null",
        quiet=True,
    )
    if rc == 0 and out:
        print(f"  Status: healthy")
        try:
            import json

            data = json.loads(out)
            if "version" in data:
                print(f"  Version: {data['version']}")
        except Exception:
            pass
    else:
        # Try via systemd
        rc2, svc_out, _ = ssh_run(
            host,
            "systemctl is-active tether 2>/dev/null || systemctl --user is-active tether 2>/dev/null",
            quiet=True,
        )
        svc_state = svc_out.strip() if svc_out.strip() else "unknown"
        print(f"  Status: unreachable (service: {svc_state})")
        sys.exit(1)


def _run_server_upgrade(args: argparse.Namespace) -> None:
    """Handle ``tether server upgrade <name>``."""
    from tether.servers import get_server
    from tether.server_init import ssh_run

    name = args.name
    profile = get_server(name)
    if profile is None:
        print(
            f"Error: unknown server '{name}'. Check ~/.config/tether/servers.yaml.",
            file=sys.stderr,
        )
        sys.exit(1)

    host = profile.get("host", name)

    print(f"Upgrading tether-ai on {name} ({host})...")
    rc, out, stderr = ssh_run(
        host,
        "~/.local/bin/pipx upgrade tether-ai 2>&1 || pipx upgrade tether-ai 2>&1",
        timeout=180,
    )
    if rc != 0:
        print(f"Error: upgrade failed: {stderr}", file=sys.stderr)
        sys.exit(1)
    if out:
        print(out)
    print("Restarting service...")
    rc2, _, _ = ssh_run(
        host,
        f"sudo systemctl restart tether 2>/dev/null || systemctl --user restart tether 2>/dev/null",
        quiet=True,
    )
    if rc2 == 0:
        print("Service restarted.")
    else:
        print("Could not restart the service automatically. Restart it manually.")


def _run_server_logs(args: argparse.Namespace) -> None:
    """Handle ``tether server logs <name>``."""
    from tether.servers import get_server
    from tether.server_init import ssh_run

    name = args.name
    profile = get_server(name)
    if profile is None:
        print(
            f"Error: unknown server '{name}'. Check ~/.config/tether/servers.yaml.",
            file=sys.stderr,
        )
        sys.exit(1)

    host = profile.get("host", name)
    lines = getattr(args, "lines", 50)
    follow = getattr(args, "follow", False)

    follow_flag = "-f " if follow else ""
    journalctl_cmd = (
        f"sudo journalctl -u tether {follow_flag}-n {lines} 2>/dev/null || "
        f"journalctl --user -u tether {follow_flag}-n {lines} 2>/dev/null"
    )

    if follow:
        # Stream interactively
        user = profile.get("user")
        target = f"{user}@{host}" if user else host
        cmd = ["ssh", "-o", "BatchMode=yes", target, journalctl_cmd]
        try:
            subprocess.run(cmd)
        except KeyboardInterrupt:
            pass
    else:
        rc, out, _ = ssh_run(host, journalctl_cmd, timeout=30)
        if out:
            print(out)


if __name__ == "__main__":
    main()
