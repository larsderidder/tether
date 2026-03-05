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

from tether.servers import (
    get_active_context_server,
    get_default_server,
    get_server,
)


def _apply_connection_args(args: argparse.Namespace) -> None:
    """Apply --host/--port/--token/--server flags to the process environment.

    Precedence (highest to lowest):
    1. Explicit ``--host``, ``--port``, ``--token`` CLI flags
    2. ``--server <name>`` profile from ``~/.config/tether/servers.yaml``
    3. Active context from ``~/.config/tether/context``
    4. ``default`` key in servers.yaml
    5. Existing env vars / config file (already loaded by ``load_config``)
    6. Built-in defaults (127.0.0.1:8787)
    """
    host = getattr(args, "remote_host", None)
    port = getattr(args, "remote_port", None)
    token = getattr(args, "remote_token", None)
    server_name = getattr(args, "server", None)

    # Resolve a named server profile first; explicit flags will override it.
    profile: dict[str, str] | None = None
    if server_name:
        profile = get_server(server_name)
        if profile is None:
            print(
                f"Error: unknown server '{server_name}'. "
                "Check ~/.config/tether/servers.yaml.",
                file=sys.stderr,
            )
            sys.exit(1)
    elif not host and not port and not token:
        # No explicit connection flags; check active context, then default.
        ctx_name, ctx_profile = get_active_context_server()
        if ctx_name and ctx_profile:
            profile = ctx_profile
        elif ctx_name and ctx_profile is None:
            print(
                f"Error: active context '{ctx_name}' not found in "
                "~/.config/tether/servers.yaml.",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            profile = get_default_server()

    if profile:
        if "host" in profile and not host:
            os.environ["TETHER_AGENT_HOST"] = profile["host"]
        if "port" in profile and not port:
            os.environ["TETHER_AGENT_PORT"] = profile["port"]
        if "token" in profile and not token:
            os.environ["TETHER_AGENT_TOKEN"] = profile["token"]

    # Explicit CLI flags always win.
    if host:
        os.environ["TETHER_AGENT_HOST"] = host
    if port is not None:
        os.environ["TETHER_AGENT_PORT"] = str(port)
    if token:
        os.environ["TETHER_AGENT_TOKEN"] = token


def main(argv: list[str] | None = None) -> None:
    """Main CLI entry point (``tether`` command)."""
    parser = argparse.ArgumentParser(
        prog="tether",
        description="Tether \u2014 control plane for AI coding agents",
    )

    # Global connection flags (apply to all client subcommands).
    parser.add_argument(
        "--host", "-H",
        dest="remote_host",
        metavar="HOST",
        help="Remote Tether server hostname or IP (overrides TETHER_AGENT_HOST)",
    )
    parser.add_argument(
        "--port", "-P",
        dest="remote_port",
        type=int,
        metavar="PORT",
        help="Remote Tether server port (overrides TETHER_AGENT_PORT)",
    )
    parser.add_argument(
        "--token",
        dest="remote_token",
        metavar="TOKEN",
        help="Bearer token for remote server auth (overrides TETHER_AGENT_TOKEN)",
    )
    parser.add_argument(
        "--server", "-S",
        dest="server",
        metavar="NAME",
        help="Use a named server profile from ~/.config/tether/servers.yaml",
    )

    sub = parser.add_subparsers(dest="command")

    # tether start
    start_parser = sub.add_parser("start", help="Start the Tether server")
    start_parser.add_argument("--host", dest="bind_host", help="Host to bind to")
    start_parser.add_argument("--port", dest="bind_port", type=int, help="Port to bind to")
    start_parser.add_argument(
        "--dev", action="store_true", help="Enable dev mode (no auth required)"
    )

    # tether init
    sub.add_parser("init", help="Interactive setup wizard")

    # tether status
    sub.add_parser("status", help="Server health and session summary")

    # tether verify
    sub.add_parser("verify", help="Check that the server is reachable and healthy")

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
        default=None,
        help="Working directory (default: current directory, unless --clone is used)",
    )
    new_parser.add_argument(
        "--template", "-t",
        metavar="NAME_OR_PATH",
        help="Session template name or path (overrides can be mixed with other flags)",
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
    new_parser.add_argument(
        "--clone", "-c",
        dest="clone_url",
        metavar="URL",
        help="Git repo URL to clone as the session workspace",
    )
    new_parser.add_argument(
        "--branch", "-b",
        dest="clone_branch",
        metavar="BRANCH",
        help="Branch to checkout (only valid with --clone or template)",
    )
    new_parser.add_argument(
        "--shallow",
        action="store_true",
        help="Perform a shallow clone (only valid with --clone or template)",
    )
    new_parser.add_argument(
        "--auto-branch",
        action="store_true",
        dest="auto_branch",
        help="Create a working branch after clone (only valid with --clone or template)",
    )

    # tether templates
    templates_parser = sub.add_parser("templates", help="Manage session templates")
    templates_sub = templates_parser.add_subparsers(dest="templates_command")

    templates_sub.add_parser("list", help="List available templates")

    templates_show_p = templates_sub.add_parser("show", help="Show template contents")
    templates_show_p.add_argument("name", help="Template name or path")

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

    # tether git <subcommand>
    git_parser = sub.add_parser("git", help="Git operations on a session workspace")
    git_sub = git_parser.add_subparsers(dest="git_command")

    # tether git status <session-id>
    git_status_p = git_sub.add_parser("status", help="Show branch, changes, last commit")
    git_status_p.add_argument("session_id", help="Session ID (prefix is fine)")

    # tether git log <session-id> [-n N]
    git_log_p = git_sub.add_parser("log", help="Show recent commits")
    git_log_p.add_argument("session_id", help="Session ID (prefix is fine)")
    git_log_p.add_argument(
        "-n", "--count", type=int, default=10, metavar="N",
        help="Number of commits to show (default: 10)",
    )

    # tether git diff <session-id>
    git_diff_p = git_sub.add_parser("diff", help="Show full git diff")
    git_diff_p.add_argument("session_id", help="Session ID (prefix is fine)")

    # tether git commit <session-id> -m <message>
    git_commit_p = git_sub.add_parser("commit", help="Commit all changes")
    git_commit_p.add_argument("session_id", help="Session ID (prefix is fine)")
    git_commit_p.add_argument(
        "--message", "-m", required=True, metavar="MSG",
        help="Commit message",
    )

    # tether git push <session-id>
    git_push_p = git_sub.add_parser("push", help="Push commits to remote")
    git_push_p.add_argument("session_id", help="Session ID (prefix is fine)")
    git_push_p.add_argument(
        "--remote", default="origin", help="Remote name (default: origin)"
    )
    git_push_p.add_argument(
        "--branch", metavar="BRANCH", help="Branch to push (default: current branch)"
    )

    # tether git branch <session-id> <name>
    git_branch_p = git_sub.add_parser("branch", help="Create and checkout a new branch")
    git_branch_p.add_argument("session_id", help="Session ID (prefix is fine)")
    git_branch_p.add_argument("name", help="New branch name")
    git_branch_p.add_argument(
        "--no-checkout", action="store_true",
        help="Create branch without switching to it",
    )

    # tether git checkout <session-id> <branch>
    git_checkout_p = git_sub.add_parser("checkout", help="Checkout an existing branch")
    git_checkout_p.add_argument("session_id", help="Session ID (prefix is fine)")
    git_checkout_p.add_argument("branch", help="Branch to checkout")

    # tether git pr <session-id> -t <title>
    git_pr_p = git_sub.add_parser("pr", help="Create a pull/merge request")
    git_pr_p.add_argument("session_id", help="Session ID (prefix is fine)")
    git_pr_p.add_argument("--title", "-t", required=True, metavar="TITLE", help="PR title")
    git_pr_p.add_argument("--body", "-b", default="", metavar="BODY", help="PR description")
    git_pr_p.add_argument("--base", metavar="BRANCH", help="Target branch (default: repo default)")
    git_pr_p.add_argument("--draft", action="store_true", help="Create as a draft PR")
    git_pr_p.add_argument(
        "--no-push", action="store_true", dest="no_push",
        help="Do not auto-push before creating the PR",
    )

    # tether workspaces [--stale]
    workspaces_parser = sub.add_parser(
        "workspaces", help="List managed workspaces with disk usage"
    )
    workspaces_parser.add_argument(
        "--stale",
        action="store_true",
        help="Show only stale/orphaned workspaces",
    )

    # tether workspaces clean
    workspaces_sub = workspaces_parser.add_subparsers(dest="workspaces_command")
    workspaces_sub.add_parser(
        "clean", help="Remove orphaned workspace directories"
    )

    # tether context [list|use <name>]
    context_parser = sub.add_parser(
        "context", help="Show or switch the active server context"
    )
    context_sub = context_parser.add_subparsers(dest="context_command")
    context_sub.add_parser("list", help="List all available contexts")
    context_use_p = context_sub.add_parser(
        "use", help="Switch to a named context"
    )
    context_use_p.add_argument(
        "context_name", help="Context name (use 'local' for local server)"
    )

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
    elif args.command == "templates":
        _run_templates(args)
    elif args.command == "context":
        _run_context(args)
    elif args.command == "server":
        _run_server(args)
    elif args.command in (
        "status", "verify", "open", "list", "attach", "new", "input", "interrupt", "delete", "sync",
        "watch", "git", "workspaces",
    ):
        _apply_connection_args(args)
        _run_client(args)
    else:
        parser.print_help()
        sys.exit(1)


def _run_start(args: argparse.Namespace) -> None:
    """Handle ``tether start``."""
    # Apply CLI flag overrides BEFORE loading config
    if args.bind_host:
        os.environ["TETHER_AGENT_HOST"] = args.bind_host
    if args.bind_port:
        os.environ["TETHER_AGENT_PORT"] = str(args.bind_port)
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


def _run_templates(args: argparse.Namespace) -> None:
    """Handle ``tether templates`` subcommands (no server connection needed)."""
    from tether.cli_client import cmd_templates_list, cmd_templates_show

    cmd = getattr(args, "templates_command", None)
    if cmd == "list":
        cmd_templates_list()
    elif cmd == "show":
        cmd_templates_show(args.name)
    else:
        print("Usage: tether templates <list|show>", file=sys.stderr)
        sys.exit(1)


def _run_context(args: argparse.Namespace) -> None:
    """Handle ``tether context`` subcommands (no server connection needed)."""
    from tether.servers import (
        get_active_context,
        get_server,
        list_contexts,
        set_active_context,
    )

    cmd = getattr(args, "context_command", None)

    if cmd == "list":
        contexts = list_contexts()
        # Column widths
        max_name = max(len(c["name"]) for c in contexts)
        max_host = max(len(c["host"]) for c in contexts)
        for ctx in contexts:
            marker = "*" if ctx["active"] else " "
            name = ctx["name"].ljust(max_name)
            host_port = f'{ctx["host"]}:{ctx["port"]}'.ljust(max_host + 6)
            print(f"  {marker} {name}  {host_port}")
    elif cmd == "use":
        name = args.context_name
        if name != "local":
            profile = get_server(name)
            if profile is None:
                print(
                    f"Error: unknown context '{name}'. "
                    "Check ~/.config/tether/servers.yaml.",
                    file=sys.stderr,
                )
                sys.exit(1)
        set_active_context(name)
        if name == "local":
            print("Switched to local context.")
        else:
            profile = get_server(name) or {}
            host = profile.get("host", "?")
            port = profile.get("port", "8787")
            print(f"Switched to context '{name}' ({host}:{port}).")
    else:
        # No subcommand: show active context
        active = get_active_context()
        if active is None:
            print("local")
        else:
            profile = get_server(active)
            if profile:
                host = profile.get("host", "?")
                port = profile.get("port", "8787")
                print(f"{active} ({host}:{port})")
            else:
                print(f"{active} (not found in servers.yaml)")


def _run_client(args: argparse.Namespace) -> None:
    """Handle client subcommands that talk to a running server."""
    # Load config so we pick up token, host, port from .env files
    from tether.config import load_config

    load_config()

    from tether.cli_client import (
        cmd_attach,
        cmd_delete,
        cmd_git_branch,
        cmd_git_checkout,
        cmd_git_commit,
        cmd_git_diff,
        cmd_git_log,
        cmd_git_pr,
        cmd_git_push,
        cmd_git_status,
        cmd_input,
        cmd_interrupt,
        cmd_list,
        cmd_list_external,
        cmd_new,
        cmd_open,
        cmd_status,
        cmd_sync,
        cmd_verify,
        cmd_watch,
    )

    if args.command == "status":
        cmd_status()
    elif args.command == "verify":
        cmd_verify()
    elif args.command == "open":
        cmd_open()
    elif args.command == "list":
        if args.external:
            cmd_list_external(args.directory, args.runner_type)
        else:
            cmd_list(state=args.state, directory=args.directory)
    elif args.command == "new":
        template_name = getattr(args, "template", None)
        clone_url = getattr(args, "clone_url", None)
        clone_branch = getattr(args, "clone_branch", None)
        shallow = getattr(args, "shallow", False)
        auto_branch = getattr(args, "auto_branch", False)

        if template_name:
            # Template path: resolve template, then apply explicit flag overrides
            from tether.templates import TemplateError, resolve_template

            try:
                resolved = resolve_template(
                    template_name,
                    overrides={
                        "clone_url": clone_url or None,
                        "clone_branch": clone_branch or None,
                        "adapter": args.adapter or None,
                        "platform": args.platform or None,
                        "auto_branch": auto_branch or None,
                        "shallow": shallow or None,
                        "directory": args.directory or None,
                    },
                )
            except TemplateError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)

            cmd_new(
                directory=resolved.get("directory"),
                adapter=resolved.get("adapter"),
                prompt=args.prompt,
                platform=resolved.get("platform"),
                clone_url=resolved.get("clone_url"),
                clone_branch=resolved.get("clone_branch"),
                shallow=bool(resolved.get("shallow")),
                auto_branch=bool(resolved.get("auto_branch")),
                approval_mode=resolved.get("approval_mode"),
            )
        else:
            # No template — existing behaviour
            # --branch / --shallow / --auto-branch without --clone is a user error
            if not clone_url and (clone_branch or shallow or auto_branch):
                print(
                    "Error: --branch, --shallow, and --auto-branch require --clone.",
                    file=sys.stderr,
                )
                sys.exit(1)

            if clone_url:
                # --clone and a positional directory are mutually exclusive
                if args.directory is not None:
                    print(
                        "Error: --clone and a directory argument are mutually exclusive.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                cmd_new(
                    directory=None,
                    adapter=args.adapter,
                    prompt=args.prompt,
                    platform=args.platform,
                    clone_url=clone_url,
                    clone_branch=clone_branch,
                    shallow=shallow,
                    auto_branch=auto_branch,
                )
            else:
                directory = os.path.abspath(args.directory or ".")
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
    elif args.command == "git":
        git_cmd = getattr(args, "git_command", None)
        if not git_cmd:
            print("Error: specify a git subcommand (status, log, diff, commit, push, branch, checkout).", file=sys.stderr)
            sys.exit(1)
        if git_cmd == "status":
            cmd_git_status(args.session_id)
        elif git_cmd == "log":
            cmd_git_log(args.session_id, count=args.count)
        elif git_cmd == "diff":
            cmd_git_diff(args.session_id)
        elif git_cmd == "commit":
            cmd_git_commit(args.session_id, args.message)
        elif git_cmd == "push":
            cmd_git_push(args.session_id, remote=args.remote, branch=getattr(args, "branch", None))
        elif git_cmd == "branch":
            cmd_git_branch(args.session_id, args.name, checkout=not args.no_checkout)
        elif git_cmd == "checkout":
            cmd_git_checkout(args.session_id, args.branch)
        elif git_cmd == "pr":
            cmd_git_pr(
                args.session_id,
                title=args.title,
                body=args.body,
                base=getattr(args, "base", None),
                draft=args.draft,
                auto_push=not args.no_push,
            )
        else:
            print(f"Error: unknown git subcommand '{git_cmd}'.", file=sys.stderr)
            sys.exit(1)
    elif args.command == "workspaces":
        ws_cmd = getattr(args, "workspaces_command", None)
        from tether.cli_client import cmd_workspaces, cmd_workspaces_clean

        if ws_cmd == "clean":
            cmd_workspaces_clean()
        else:
            cmd_workspaces(stale_only=getattr(args, "stale", False))


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
