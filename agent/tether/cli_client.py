"""HTTP client for Tether CLI commands.

Thin wrapper around the REST API for use by CLI subcommands. Talks to
a running Tether server over HTTP; never imports server internals.
"""

from __future__ import annotations

import json
import os
import sys
import webbrowser
from itertools import groupby
from typing import Callable

import httpx


def _base_url() -> str:
    """Return the base URL for the Tether server."""
    host = os.environ.get("TETHER_AGENT_HOST", "127.0.0.1")
    port = os.environ.get("TETHER_AGENT_PORT", "8787")
    # 0.0.0.0 is not connectable; use localhost instead
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def _auth_headers() -> dict[str, str]:
    """Return auth headers if a token is configured."""
    token = os.environ.get("TETHER_AGENT_TOKEN", "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=_base_url(),
        headers=_auth_headers(),
        timeout=30.0,
    )


# Short aliases for runner types accepted by the API
_RUNNER_TYPE_ALIASES: dict[str, str] = {
    "claude": "claude_code",
    "cc": "claude_code",
}


def _normalize_runner_type(value: str) -> str:
    """Expand short runner type aliases to API values."""
    return _RUNNER_TYPE_ALIASES.get(value, value)


def _handle_connection_error() -> None:
    """Print a friendly message when the server is unreachable."""
    print("Error: Cannot connect to the Tether server.", file=sys.stderr)
    print("Is it running? Start it with: tether start", file=sys.stderr)
    sys.exit(1)


def _check_response(resp: httpx.Response) -> None:
    """Check an HTTP response and exit with a friendly message on error."""
    if resp.status_code < 400:
        return
    msg = ""
    try:
        body = resp.json()
        # Server error format: {"error": {"code": ..., "message": ...}}
        error = body.get("error", {})
        if isinstance(error, dict):
            msg = error.get("message", "")
        # FastAPI validation format: {"detail": ...}
        if not msg:
            detail = body.get("detail", {})
            if isinstance(detail, dict):
                msg = detail.get("message", "") or detail.get("msg", "")
            elif isinstance(detail, str):
                msg = detail
            elif isinstance(detail, list) and detail:
                msg = detail[0].get("msg", "")
    except Exception:
        pass
    if not msg:
        msg = resp.text or f"HTTP {resp.status_code}"
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _get_json(path: str, *, params: dict[str, str] | None = None) -> list | dict:
    """GET a JSON response, exiting on HTTP errors."""
    with _client() as c:
        resp = c.get(path, params=params)
        _check_response(resp)
        return resp.json()


def _detect_platform() -> str | None:
    """Return the single active bridge platform, or None.

    If exactly one bridge is running, return its name so callers can
    auto-bind sessions without requiring an explicit --bridge flag.
    Returns None when zero or multiple bridges are active.
    """
    try:
        with _client() as c:
            resp = c.get("/api/status/bridges")
            if resp.status_code != 200:
                return None
            data = resp.json()
        running = [
            b["platform"]
            for b in data.get("bridges", [])
            if b.get("status") == "running"
        ]
        return running[0] if len(running) == 1 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Context banner
# ---------------------------------------------------------------------------


def _print_context_banner() -> None:
    """Print the active context as a one-liner when using a remote server."""
    from tether.servers import get_active_context, get_server

    active = get_active_context()
    if active is None:
        return
    profile = get_server(active)
    if profile:
        host = profile.get("host", "?")
        port = profile.get("port", "8787")
        print(f"\u27f6 {active} ({host}:{port})")
    else:
        print(f"\u27f6 {active}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_open() -> None:
    """Open the web UI in the default browser."""
    url = _base_url()
    print(f"Opening {url}")
    webbrowser.open(url)


def cmd_setup_agents(
    agent_filter: str | None = None,
    *,
    check_only: bool = False,
    all_agents: bool = False,
) -> None:
    """Interactively install and configure agent CLIs on the remote server.

    Fetches the current agent status from the server, then for each agent
    that is not installed or not authenticated it asks the user whether to
    install / push credentials.

    By default only agents for which local credentials exist are prompted.
    Pass all_agents=True to prompt for every agent regardless.

    Args:
        agent_filter: If given, only handle that one agent by name.
        check_only:   Just print status; do not prompt for any actions.
        all_agents:   Prompt for all agents, even those without local credentials.
    """
    # Determine a human-readable server label for display purposes.
    server_label = _server_label()

    try:
        data = _get_json("/api/setup/agents")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    agents: list[dict] = data.get("agents", [])

    if agent_filter:
        agents = [a for a in agents if a["name"] == agent_filter]
        if not agents:
            print(f"Error: unknown agent '{agent_filter}'.", file=sys.stderr)
            print("Known agents: claude_code, opencode, pi", file=sys.stderr)
            sys.exit(1)

    print(f"Checking remote server ({server_label})...\n")

    # Print status table.
    _print_agents_status(agents)

    if check_only:
        return

    # Interactive provisioning loop.
    any_action = False

    for agent in agents:
        name = agent["name"]
        label = _agent_label(name)
        installed = agent.get("installed", False)
        authenticated = agent.get("authenticated", False)
        install_cmd = agent.get("install_command")
        install_requires = agent.get("install_requires")
        install_requires_met = agent.get("install_requires_met", True)
        version = agent.get("version")

        has_local_creds = _has_local_credentials(name)

        # Skip agents with no local credentials unless --all was passed.
        if not all_agents and not has_local_creds:
            continue

        # Determine if we need to do anything.
        needs_install = not installed and install_cmd
        needs_creds = installed and not authenticated and has_local_creds

        if not needs_install and not needs_creds:
            continue

        print()

        if needs_install:
            # Warn early if the required runtime is missing.
            if install_requires and not install_requires_met:
                print(
                    f"  Cannot install {label}: '{install_requires}' is not installed on the server."
                )
                print(f"  Install it first, then re-run setup.")
                continue

            if not _prompt(f"Install {label}?"):
                continue

            any_action = True
            print(f"  Installing {label} on {server_label}...")

            try:
                resp_data = _post_json(f"/api/setup/agents/{name}/install", {})
            except (httpx.ConnectError, httpx.ConnectTimeout):
                _handle_connection_error()
                return

            new_version = resp_data.get("version")
            if new_version:
                print(f"  Installed {label} v{new_version}")
            else:
                print(f"  Installed {label}")

            # Re-probe: now offer credentials if applicable.
            installed = True
            version = new_version

        # Offer to push credentials if we have them locally.
        creds_files = _read_local_credentials(name)
        if installed and creds_files and not authenticated:
            if not _prompt(f"Push {label} credentials from local machine?"):
                continue

            any_action = True
            print(f"  Pushing credentials to {server_label}...")

            try:
                _post_json(
                    f"/api/setup/agents/{name}/credentials",
                    {"files": creds_files},
                )
            except (httpx.ConnectError, httpx.ConnectTimeout):
                _handle_connection_error()
                return

            print("  Credentials installed")

        # Verify.
        if installed:
            print(f"  Verifying {label}...")
            try:
                verify = _post_json(f"/api/setup/agents/{name}/verify", {})
            except (httpx.ConnectError, httpx.ConnectTimeout):
                _handle_connection_error()
                return

            if verify.get("ok"):
                print(f"  {label}: authenticated \u2713")
            else:
                print(f"  {label}: {verify.get('message', 'verification failed')}")

    if any_action:
        print("\nSetup complete. You can now run:")
        print('  tether new --adapter claude_auto -m "fix the thing"')


# ---------------------------------------------------------------------------
# setup helpers
# ---------------------------------------------------------------------------


def _server_label() -> str:
    """Return a short label for the currently active server (host:port)."""
    host = os.environ.get("TETHER_AGENT_HOST", "127.0.0.1")
    port = os.environ.get("TETHER_AGENT_PORT", "8787")
    if host in ("127.0.0.1", "0.0.0.0", "localhost"):
        return "local"
    return f"{host}:{port}"


def _agent_label(name: str) -> str:
    """Return a human-readable label for an agent name."""
    labels = {
        "claude_code": "Claude Code",
        "opencode": "OpenCode",
        "pi": "pi",
    }
    return labels.get(name, name)


def _print_agents_status(agents: list[dict]) -> None:
    """Print a status table of agents."""
    label_width = max(len(_agent_label(a["name"])) for a in agents) if agents else 10
    for agent in agents:
        label = _agent_label(agent["name"]).ljust(label_width)
        if agent.get("installed"):
            ver = agent.get("version") or ""
            ver_str = f" v{ver}" if ver else ""
            auth = (
                " (authenticated)"
                if agent.get("authenticated")
                else " (not authenticated)"
            )
            status = f"installed{ver_str}{auth}"
        else:
            status = "not installed"
        print(f"  {label}  {status}")


def _has_local_credentials(agent_name: str) -> bool:
    """Return True if local credential files exist for the agent."""
    return bool(_read_local_credentials(agent_name))


def _read_local_credentials(agent_name: str) -> dict[str, str]:
    """Read local credential files for an agent and return {rel_path: content}.

    Currently only claude_code is supported (reads ~/.claude/.credentials.json).
    Returns an empty dict if no credentials are found locally.
    """
    if agent_name != "claude_code":
        return {}

    home = os.path.expanduser("~")
    creds_path = os.path.join(home, ".claude", ".credentials.json")

    if not os.path.exists(creds_path):
        return {}

    try:
        with open(creds_path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return {}

    rel = ".claude/.credentials.json"
    return {rel: content}


def _prompt(question: str, default: bool = True) -> bool:
    """Ask a yes/no question. Returns True for yes, False for no."""
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if answer == "":
        return default
    return answer in ("y", "yes")


def _post_json(path: str, body: dict) -> dict:
    """POST JSON and return the response dict, exiting on HTTP errors."""
    with _client() as c:
        resp = c.post(path, json=body)
        _check_response(resp)
        return resp.json()


def cmd_status() -> None:
    """Print server health and session summary."""
    _print_context_banner()
    try:
        h = _get_json("/api/health")
        items = _get_json("/api/sessions")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    print(f"Server:   {_base_url()} (ok)")
    print(f"Version:  {h.get('version', '?')}")
    print(f"Sessions: {len(items)}")

    # Quick breakdown by state
    by_state: dict[str, int] = {}
    for s in items:
        state = s.get("state", "?")
        by_state[state] = by_state.get(state, 0) + 1
    if by_state:
        parts = [
            f"{count} {_format_state(state)}"
            for state, count in sorted(by_state.items())
        ]
        print(f"          {', '.join(parts)}")

    # Bridge status (optional endpoint, may not exist)
    try:
        with _client() as c:
            resp = c.get("/api/bridges")
            if resp.status_code == 200:
                data = resp.json()
                active = [
                    b["platform"]
                    for b in data.get("bridges", [])
                    if b.get("status") == "running"
                ]
                if active:
                    print(f"Bridges:  {', '.join(active)}")
                else:
                    print("Bridges:  none connected")
    except Exception:
        pass


def cmd_list(
    state: str | None = None,
    directory: str | None = None,
) -> None:
    """List Tether sessions as a table."""
    _print_context_banner()
    try:
        items = _get_json("/api/sessions")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    # Client-side filtering (the API doesn't support query params for these)
    items = _filter_sessions(items, state=state, directory=directory)

    if not items:
        print("No sessions.")
        return

    _print_sessions_table(items)


def cmd_list_external(
    directory: str | None,
    runner_type: str | None,
    limit: int = 50,
) -> None:
    """List discoverable external sessions."""
    params: dict[str, str] = {"limit": str(limit)}
    if directory:
        params["directory"] = directory
    if runner_type:
        params["runner_type"] = _normalize_runner_type(runner_type)

    try:
        items = _get_json("/api/external-sessions", params=params)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    if not items:
        print("No external sessions found.")
        return

    _print_external_sessions_table(items)


def cmd_attach(
    external_id: str | None,
    runner_type: str,
    directory: str,
    platform: str | None = None,
) -> None:
    """Attach an external session to Tether.

    Supports ID prefixes: resolves against the external sessions list
    when the given ID doesn't match exactly. Also auto-detects the
    runner_type and directory from the matched session when possible.

    If no external_id is given, shows external sessions for the current
    directory and prompts for a selection.
    """
    try:
        # Fetch external sessions for prefix resolution or interactive pick
        ext_sessions = _get_json("/api/external-sessions", params={"limit": "200"})

        # Interactive mode: no ID given, pick from current directory
        if not external_id:
            cwd = os.getcwd()
            local = [
                s
                for s in ext_sessions
                if s.get("directory", "").rstrip("/") == cwd.rstrip("/")
            ]
            if not local:
                print(f"No external sessions found in {cwd}")
                print("Usage: tether attach <session-id>")
                sys.exit(1)

            print(f"External sessions in {os.path.basename(cwd)}:\n")
            for i, s in enumerate(local, 1):
                rtype = s.get("runner_type", "?")
                running = " (running)" if s.get("is_running") else ""
                prompt = _truncate(s.get("first_prompt") or s.get("last_prompt"), 50)
                print(f"  {i}) {_short_id(s['id'])}  [{rtype}]{running}  {prompt}")

            print()
            try:
                choice = input("Pick a session (number): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(0)

            try:
                idx = int(choice) - 1
                if idx < 0 or idx >= len(local):
                    raise ValueError
            except ValueError:
                print("Invalid choice.", file=sys.stderr)
                sys.exit(1)

            match = local[idx]
            resolved_id = match["id"]
            resolved_runner_type = match.get("runner_type", runner_type)
            resolved_directory = match.get("directory", directory)
        else:
            # Prefix resolution
            resolved_id = external_id
            resolved_runner_type = _normalize_runner_type(runner_type)
            resolved_directory = directory

            match = _resolve_prefix(
                external_id,
                ext_sessions,
                label="external session",
                describe=_describe_external_session,
                allow_no_match=True,
            )
            if match:
                resolved_id = match["id"]
                resolved_runner_type = match.get("runner_type", runner_type)
                if directory == os.getcwd():
                    resolved_directory = match.get("directory", directory)

        if not platform:
            platform = _detect_platform()

        body: dict = {
            "external_id": resolved_id,
            "runner_type": resolved_runner_type,
            "directory": resolved_directory,
        }
        if platform:
            body["platform"] = platform

        with _client() as c:
            resp = c.post(
                "/api/sessions/attach",
                json=body,
            )
            _check_response(resp)
            session = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    print(f"Attached session {session['id']}")
    print(f"  Name:      {session.get('name') or '(unnamed)'}")
    print(f"  State:     {_format_state(session['state'])}")
    print(f"  Directory: {session.get('directory') or '?'}")
    if session.get("platform"):
        print(f"  Platform:  {session['platform']}")


def cmd_new(
    directory: str | None = None,
    adapter: str | None = None,
    prompt: str | None = None,
    platform: str | None = None,
    clone_url: str | None = None,
    clone_branch: str | None = None,
    shallow: bool = False,
    auto_branch: bool = False,
    approval_mode: int | None = None,
) -> None:
    """Create a new session and optionally start it with a prompt."""
    body: dict = {}
    if clone_url:
        body["clone_url"] = clone_url
        if clone_branch:
            body["clone_branch"] = clone_branch
        if shallow:
            body["shallow"] = True
        if auto_branch:
            body["auto_branch"] = True
    else:
        body["directory"] = directory or "."
    if adapter:
        body["adapter"] = adapter
    if not platform:
        platform = _detect_platform()
    if platform:
        body["platform"] = platform
    if approval_mode is not None:
        body["approval_mode"] = approval_mode

    if clone_url:
        print(f"Cloning {clone_url}...", flush=True)

    try:
        with _client() as c:
            resp = c.post("/api/sessions", json=body)
            if resp.status_code == 422:
                # Try to surface a helpful message
                try:
                    msg = (resp.json().get("error") or {}).get("message", "")
                except Exception:
                    msg = ""
                if (
                    "no default adapter" in msg.lower()
                    or "not configured" in msg.lower()
                ):
                    print(
                        "Error: No adapter specified and TETHER_DEFAULT_AGENT_ADAPTER is not set.\n"
                        "Use -a to specify one: tether new . -a claude_auto\n"
                        "Or set a default: echo 'TETHER_DEFAULT_AGENT_ADAPTER=claude_auto'"
                        " >> ~/.config/tether/config.env",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                if clone_url and ("clone" in msg.lower() or "git" in msg.lower()):
                    print(f"Error: Clone failed: {msg}", file=sys.stderr)
                    sys.exit(1)
            _check_response(resp)
            session = resp.json()

        if prompt:
            start_body: dict = {"prompt": prompt}
            if approval_mode is not None:
                start_body["approval_choice"] = approval_mode
            with _client() as c:
                resp = c.post(
                    f"/api/sessions/{session['id']}/start",
                    json=start_body,
                )
                _check_response(resp)
                session = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    print(f"Created session {session['id']}")
    print(f"  Directory: {session.get('directory') or '?'}")
    if session.get("clone_url"):
        print(f"  Cloned:    {session['clone_url']}")
    if session.get("working_branch"):
        print(f"  Branch:    {session['working_branch']}")
    print(f"  Adapter:   {session.get('adapter') or 'default'}")
    print(f"  State:     {_format_state(session['state'])}")
    if session.get("platform"):
        print(f"  Platform:  {session['platform']}")
    if prompt:
        print(f"  Started with prompt.")


def cmd_delete(session_id: str) -> None:
    """Delete a session."""
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return

    try:
        with _client() as c:
            resp = c.delete(f"/api/sessions/{session_id}")
            _check_response(resp)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    print(f"Deleted {session_id}")


def cmd_input(session_id: str, text: str) -> None:
    """Send input to a session."""
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return

    try:
        with _client() as c:
            resp = c.post(
                f"/api/sessions/{session_id}/input",
                json={"text": text},
            )
            _check_response(resp)
            session = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    print(
        f"Sent to {_short_id(session_id)} ({_format_state(session.get('state', '?'))})"
    )


def cmd_sync(session_id: str) -> None:
    """Pull new messages from an attached external session."""
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return

    try:
        with _client() as c:
            resp = c.post(f"/api/sessions/{session_id}/sync")
            _check_response(resp)
            result = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    synced = result.get("synced", 0)
    total = result.get("total", 0)
    if synced == 0:
        print(f"Already up to date ({total} message{'s' if total != 1 else ''} total)")
    else:
        print(
            f"Synced {synced} new message{'s' if synced != 1 else ''} ({total} total)"
        )


def cmd_interrupt(session_id: str) -> None:
    """Interrupt a running session."""
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return

    try:
        with _client() as c:
            resp = c.post(f"/api/sessions/{session_id}/interrupt")
            _check_response(resp)
            session = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    print(f"Session {session_id}: {session['state'].lower()}")


def cmd_watch(session_id: str) -> None:
    """Stream live output from a session to the terminal.

    Connects to the SSE event stream and prints output as it arrives.
    Press Ctrl+C to stop watching (the session continues running).
    """
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return

    url = _base_url()
    path = f"/events/sessions/{session_id}"
    headers = _auth_headers()

    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80

    import http.client
    import socket

    print(f"Watching {_short_id(session_id)}... (Ctrl+C to stop)")

    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()

        if resp.status != 200:
            body = resp.read().decode("utf-8", errors="replace")
            print(
                f"Error: server returned {resp.status}: {body[:200]}", file=sys.stderr
            )
            return

        if conn.sock:
            conn.sock.settimeout(60)

        while True:
            try:
                line = resp.fp.readline().decode("utf-8", errors="replace")
            except socket.timeout:
                continue

            if not line:
                print("\nStream closed.")
                break

            if not line.startswith("data: "):
                continue

            try:
                event = json.loads(line[6:].strip())
            except json.JSONDecodeError:
                continue

            etype = event.get("type")
            data = event.get("data", {})

            if etype == "output":
                text = data.get("text", "")
                if text:
                    print(text, end="", flush=True)

            elif etype == "session_state":
                state = data.get("state", "")
                if state in ("AWAITING_INPUT", "ERROR"):
                    print(f"\n[{_format_state(state)}]", flush=True)
                    break

            elif etype == "error":
                msg = data.get("message", "")
                print(f"\n[error: {msg}]", file=sys.stderr)
                break

    except KeyboardInterrupt:
        print("\nStopped watching.")
    except (httpx.ConnectError, ConnectionRefusedError, OSError):
        _handle_connection_error()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def cmd_verify() -> None:
    """Verify that the Tether server is reachable and healthy.

    Checks the health endpoint, authenticated API access, and bridge status.
    """
    url = _base_url()
    headers = _auth_headers()
    all_ok = True

    # 1. Health check (no auth required)
    print(f"Checking {url} ...")
    try:
        with _client() as c:
            resp = c.get("/api/health")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        print(f"  Health:     FAIL (cannot reach {url})", file=sys.stderr)
        print("\nIs the server running? Start it with: tether start")
        sys.exit(1)

    if resp.status_code == 200:
        print("  Health:     ok")
    else:
        print(f"  Health:     FAIL (HTTP {resp.status_code})", file=sys.stderr)
        all_ok = False

    # 2. Authenticated API access
    try:
        with _client() as c:
            resp = c.get("/api/sessions")
        if resp.status_code == 200:
            sessions = resp.json()
            print(f"  API:        ok ({len(sessions)} session{'s' if len(sessions) != 1 else ''})")
        elif resp.status_code == 401:
            print("  API:        FAIL (401 unauthorized, check TETHER_AGENT_TOKEN)", file=sys.stderr)
            all_ok = False
        else:
            print(f"  API:        FAIL (HTTP {resp.status_code})", file=sys.stderr)
            all_ok = False
    except (httpx.ConnectError, httpx.ConnectTimeout):
        print("  API:        FAIL (connection lost)", file=sys.stderr)
        all_ok = False

    # 3. Bridge status
    try:
        with _client() as c:
            resp = c.get("/api/status/bridges")
        if resp.status_code == 200:
            bridges = resp.json().get("bridges", [])
            running = [b for b in bridges if b["status"] == "running"]
            if running:
                names = ", ".join(b["platform"] for b in running)
                print(f"  Bridges:    {names}")
            else:
                print("  Bridges:    none configured")
        else:
            print(f"  Bridges:    unknown (HTTP {resp.status_code})")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        print("  Bridges:    unknown (connection lost)")

    if all_ok:
        print("\nAll checks passed.")
    else:
        print("\nSome checks failed.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_prefix(
    prefix: str,
    items: list[dict],
    *,
    label: str,
    describe: Callable[[dict], str],
    allow_no_match: bool = False,
) -> dict | None:
    matches = [s for s in items if s["id"].startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) == 0:
        if allow_no_match:
            return None
        print(f"Error: No {label} matching '{prefix}'", file=sys.stderr)
        sys.exit(1)
    print(
        f"Error: Ambiguous prefix '{prefix}', matches {len(matches)} {label}s:",
        file=sys.stderr,
    )
    for m in matches:
        print(f"  {describe(m)}", file=sys.stderr)
    sys.exit(1)


def _describe_external_session(session: dict) -> str:
    prompt = _truncate(session.get("first_prompt") or session.get("last_prompt"), 50)
    return f"{session['id']}  {session.get('runner_type', '')}  {prompt}"


def _resolve_session_id(prefix: str) -> str | None:
    """Resolve a session ID prefix to a full ID.

    Allows users to type just the first few characters of a session ID.
    Returns None and prints an error if the prefix is ambiguous or not found.
    """
    try:
        items = _get_json("/api/sessions")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return None

    match = _resolve_prefix(
        prefix,
        items,
        label="session",
        describe=lambda m: f"{m['id']}  {m.get('name') or ''}",
    )
    return match["id"] if match else None


def _filter_sessions(
    items: list[dict],
    *,
    state: str | None = None,
    directory: str | None = None,
) -> list[dict]:
    if state:
        state_upper = state.upper()
        items = [s for s in items if s.get("state", "").upper() == state_upper]
    if directory:
        norm_dir = os.path.abspath(directory).rstrip("/")
        items = [s for s in items if s.get("directory", "").rstrip("/") == norm_dir]
    return items


def _truncate(text: str | None, width: int) -> str:
    """Truncate text to width, adding ellipsis if needed."""
    if not text:
        return ""
    text = text.replace("\n", " ")
    if len(text) <= width:
        return text
    return text[: width - 1] + "\u2026"


def _short_id(full_id: str) -> str:
    """Return the first 12 characters of a session ID."""
    return full_id[:12]


_STATE_LABELS: dict[str, str] = {
    "CREATED": "created",
    "RUNNING": "running",
    "AWAITING_INPUT": "awaiting input",
    "INTERRUPTING": "stopping",
    "ERROR": "error",
}


def _format_state(state: str) -> str:
    """Format a session state for display."""
    return _STATE_LABELS.get(state.upper(), state.lower())


def _print_table(headers: list[str], widths: list[int], rows: list[list[str]]) -> None:
    header_line = " ".join(
        f"{header:<{width}}" for header, width in zip(headers, widths)
    )
    rule_line = " ".join("─" * width for width in widths)
    print(header_line)
    print(rule_line)
    for row in rows:
        print(" ".join(f"{value:<{width}}" for value, width in zip(row, widths)))


def _print_sessions_table(items: list[dict]) -> None:
    """Print a formatted table of Tether sessions."""
    state_order = {
        "RUNNING": 0,
        "AWAITING_INPUT": 1,
        "INTERRUPTING": 2,
        "CREATED": 3,
        "ERROR": 4,
    }
    # Sort by state priority, then most recent activity first within each state
    items.sort(
        key=lambda s: (state_order.get(s["state"], 9), s.get("last_activity_at") or ""),
        reverse=False,
    )
    # Within each state group, reverse activity order so newest is first
    sorted_items: list[dict] = []
    for _, group in groupby(items, key=lambda s: state_order.get(s["state"], 9)):
        sorted_items.extend(
            sorted(group, key=lambda s: s.get("last_activity_at") or "", reverse=True)
        )
    items = sorted_items

    # Use terminal width for directory column, minimum 30, maximum 50
    try:
        term_width = os.get_terminal_size().columns
    except OSError:
        term_width = 80
    dir_width = max(30, min(50, term_width - 12 - 16 - 32 - 6))

    rows: list[list[str]] = []
    for s in items:
        rows.append(
            [
                _short_id(s["id"]),
                _format_state(s["state"]),
                _truncate(s.get("name"), 30),
                _truncate(s.get("directory"), dir_width),
            ]
        )

    _print_table(["ID", "STATE", "NAME", "DIRECTORY"], [12, 16, 30, dir_width], rows)


def _print_external_sessions_table(items: list[dict]) -> None:
    """Print a formatted table of external sessions."""
    rows: list[list[str]] = []
    for s in items:
        rows.append(
            [
                _short_id(s["id"]),
                s.get("runner_type", "?"),
                "yes" if s.get("is_running") else "no",
                _truncate(s.get("first_prompt") or s.get("last_prompt"), 30),
                _truncate(s.get("directory"), 30),
            ]
        )

    _print_table(
        ["ID", "TYPE", "RUNNING", "PROMPT", "DIRECTORY"],
        [12, 13, 9, 30, 30],
        rows,
    )


# ---------------------------------------------------------------------------
# Git subcommands
# ---------------------------------------------------------------------------


def cmd_git_status(session_id: str) -> None:
    """Show git status for a session's workspace."""
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return

    try:
        data = _get_json(f"/api/sessions/{session_id}/git")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    branch = data.get("branch") or "(detached HEAD)"
    remote_branch = data.get("remote_branch")
    ahead = data.get("ahead", 0)
    behind = data.get("behind", 0)
    dirty = data.get("dirty", False)
    staged = data.get("staged_count", 0)
    unstaged = data.get("unstaged_count", 0)
    untracked = data.get("untracked_count", 0)
    last = data.get("last_commit")
    remote_url = data.get("remote_url")

    print(f"Branch:  {branch}", end="")
    if remote_branch:
        tracking = f"  (tracking {remote_branch}"
        if ahead or behind:
            tracking += f", ahead {ahead}, behind {behind}"
        tracking += ")"
        print(tracking, end="")
    print()
    if remote_url:
        print(f"Remote:  {remote_url}")
    print(f"Status:  {'dirty' if dirty else 'clean'}", end="")
    if dirty:
        parts = []
        if staged:
            parts.append(f"{staged} staged")
        if unstaged:
            parts.append(f"{unstaged} unstaged")
        if untracked:
            parts.append(f"{untracked} untracked")
        print(f"  ({', '.join(parts)})", end="")
    print()
    if last:
        ts = last.get("timestamp", "")[:19].replace("T", " ")
        print(f"Last:    {last['hash']}  {last['message']}  ({last['author']}, {ts})")


def cmd_git_log(session_id: str, count: int = 10) -> None:
    """Show recent commits for a session's workspace."""
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return

    try:
        commits = _get_json(f"/api/sessions/{session_id}/git/log", params={"count": str(count)})
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    if not commits:
        print("No commits found.")
        return

    for c in commits:
        ts = c.get("timestamp", "")[:10]
        print(f"{c['hash']}  {ts}  {c['message']}  ({c['author']})")


def cmd_git_diff(session_id: str) -> None:
    """Show the full git diff for a session's workspace."""
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return

    try:
        data = _get_json(f"/api/sessions/{session_id}/diff")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    diff = data.get("diff", "") if isinstance(data, dict) else ""
    if diff:
        print(diff)
    else:
        print("No changes.")


def cmd_git_commit(session_id: str, message: str) -> None:
    """Commit all changes in a session's workspace."""
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return

    try:
        with _client() as c:
            resp = c.post(
                f"/api/sessions/{session_id}/git/commit",
                json={"message": message},
            )
            _check_response(resp)
            commit = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    print(f"Committed {commit['hash']}: {commit['message']}")


def cmd_git_push(session_id: str, remote: str = "origin", branch: str | None = None) -> None:
    """Push commits from a session's workspace to a remote."""
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return

    body: dict = {"remote": remote}
    if branch:
        body["branch"] = branch

    try:
        with _client() as c:
            resp = c.post(f"/api/sessions/{session_id}/git/push", json=body)
            _check_response(resp)
            result = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    pushed_branch = result.get("branch", branch or "")
    pushed_remote = result.get("remote", remote)
    print(f"Pushed {pushed_branch} to {pushed_remote}")


def cmd_git_branch(session_id: str, name: str, checkout: bool = True) -> None:
    """Create a new branch in a session's workspace."""
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return

    try:
        with _client() as c:
            resp = c.post(
                f"/api/sessions/{session_id}/git/branch",
                json={"name": name, "checkout": checkout},
            )
            _check_response(resp)
            result = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    action = "Created and checked out" if checkout else "Created"
    print(f"{action} branch {result['branch']}")


def cmd_git_pr(
    session_id: str,
    title: str,
    body: str = "",
    base: str | None = None,
    draft: bool = False,
    auto_push: bool = True,
) -> None:
    """Create a pull request or merge request from the session's working branch."""
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return

    payload: dict = {"title": title, "body": body, "draft": draft, "auto_push": auto_push}
    if base:
        payload["base"] = base

    try:
        with _client() as c:
            resp = c.post(
                f"/api/sessions/{session_id}/git/pr",
                json=payload,
                timeout=120.0,
            )
            _check_response(resp)
            result = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    draft_label = " (draft)" if result.get("draft") else ""
    forge = result.get("forge", "")
    print(f"{'PR' if forge == 'github' else 'MR'} created{draft_label}: {result['url']}")


def cmd_git_checkout(session_id: str, branch: str) -> None:
    """Checkout an existing branch in a session's workspace."""
    session_id = _resolve_session_id(session_id)
    if not session_id:
        return

    try:
        with _client() as c:
            resp = c.post(
                f"/api/sessions/{session_id}/git/checkout",
                json={"branch": branch},
            )
            _check_response(resp)
            result = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    print(f"Switched to branch {result['branch']}")


# ---------------------------------------------------------------------------
# Template commands (no server required)
# ---------------------------------------------------------------------------


def cmd_templates_list() -> None:
    """List all available session templates."""
    from tether.templates import list_templates

    templates = list_templates()
    if not templates:
        print("No templates found.")
        print(
            "Create a template at ~/.config/tether/templates/<name>.yaml "
            "or .tether/templates/<name>.yaml in your project."
        )
        return

    print(f"{'Name':<30}  {'Source'}")
    print("-" * 70)
    for t in templates:
        print(f"{t['name']:<30}  {t['source']}")


def cmd_templates_show(name_or_path: str) -> None:
    """Show the contents of a template."""
    from tether.templates import TemplateError, find_template, load_template

    path = find_template(name_or_path)
    if path is None:
        print(f"Error: template '{name_or_path}' not found.", file=sys.stderr)
        sys.exit(1)

    try:
        data = load_template(path)
    except TemplateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Template: {path}")
    print("-" * 50)
    for key, value in sorted(data.items()):
        print(f"  {key}: {value}")


# ---------------------------------------------------------------------------
# Workspace commands
# ---------------------------------------------------------------------------


def _fmt_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def cmd_workspaces(stale_only: bool = False) -> None:
    """List managed workspaces with disk usage."""
    params = {}
    if stale_only:
        params["stale_only"] = "true"

    try:
        with _client() as c:
            resp = c.get("/api/status/workspaces", params=params)
            _check_response(resp)
            data = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    workspaces = data.get("workspaces", [])
    total_bytes = data.get("total_bytes", 0)
    orphan_count = data.get("orphan_count", 0)
    warning = data.get("warning")

    if not workspaces:
        print("No managed workspaces found.")
        return

    print(f"{'Session':<24}  {'State':<16}  {'Size':>8}  {'Orphan':<6}  Path")
    print("-" * 90)
    for ws in workspaces:
        sid = ws["session_id"][:22]
        state = (ws.get("session_state") or "—")[:14]
        size = _fmt_bytes(ws["size_bytes"])
        orphan = "yes" if ws.get("is_orphan") else ""
        path = ws.get("path", "")
        print(f"{sid:<24}  {state:<16}  {size:>8}  {orphan:<6}  {path}")

    print()
    print(f"Total: {_fmt_bytes(total_bytes)} across {len(workspaces)} workspace(s)")
    if orphan_count:
        print(f"Orphaned: {orphan_count} (run 'tether workspaces clean' to remove)")
    if warning:
        print(f"⚠️  {warning}")


def cmd_workspaces_clean() -> None:
    """Remove orphaned workspace directories."""
    try:
        with _client() as c:
            resp = c.delete("/api/status/workspaces/orphans")
            _check_response(resp)
            data = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    removed = data.get("removed", 0)
    errors = data.get("errors", [])

    if removed == 0 and not errors:
        print("No orphaned workspaces found.")
        return

    if removed:
        print(f"Removed {removed} orphaned workspace(s).")
    for err in errors:
        print(f"  Error: {err}", file=sys.stderr)


def _create_discord_channel(
    bot_token: str,
    guild_id: str,
    name: str,
) -> str:
    """Create a text channel in a Discord guild using the bot token.

    Requires the bot to have 'Manage Channels' permission in the guild.

    Returns the new channel ID as a string.
    Raises RuntimeError on failure.
    """
    import httpx

    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }

    # Create a private text channel.
    payload = {
        "name": name,
        "type": 0,  # 0 = GUILD_TEXT
    }

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            f"https://discord.com/api/v10/guilds/{guild_id}/channels",
            headers=headers,
            json=payload,
        )

    if resp.status_code not in (200, 201):
        try:
            err = resp.json().get("message", resp.text)
        except Exception:
            err = resp.text
        raise RuntimeError(f"Discord API error {resp.status_code}: {err}")

    data = resp.json()
    return str(data["id"])


def _get_discord_guild_id(bot_token: str, channel_id: str) -> str:
    """Look up the guild ID for a given channel ID via the Discord API.

    Returns the guild ID string.
    Raises RuntimeError on failure.
    """
    import httpx

    headers = {"Authorization": f"Bot {bot_token}"}

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(
            f"https://discord.com/api/v10/channels/{channel_id}",
            headers=headers,
        )

    if resp.status_code == 200:
        return str(resp.json()["guild_id"])
    elif resp.status_code == 403:
        raise RuntimeError("Bot lacks permission to read the channel.")
    else:
        try:
            err = resp.json().get("message", resp.text)
        except Exception:
            err = resp.text
        raise RuntimeError(f"Discord API error {resp.status_code}: {err}")


def _read_local_config_env() -> dict[str, str]:
    """Read ~/.config/tether/config.env and return key/value pairs."""
    from pathlib import Path

    config_path = Path.home() / ".config" / "tether" / "config.env"
    if not config_path.exists():
        return {}
    result: dict[str, str] = {}
    for line in config_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def cmd_setup_bridge(bridge_name: str) -> None:
    """Interactively configure a messaging bridge on the remote server.

    If the local config.env already has credentials for the bridge, offers
    to reuse them. Individual values can be overridden by typing a new value,
    or accepted as-is by pressing Enter.
    """
    server_label = _server_label()

    # Fetch the required vars from the server.
    try:
        data = _get_json(f"/api/setup/bridge/vars/{bridge_name}")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    bridge_vars: list[dict] = data.get("vars", [])

    # Read local config to detect existing credentials.
    local_config = _read_local_config_env()
    local_keys = {var["key"] for var in bridge_vars if var["key"] in local_config}

    print(f"Configuring {bridge_name} bridge on {server_label}...")
    print()

    env: dict[str, str] = {}

    if local_keys == {var["key"] for var in bridge_vars if var.get("required", True)}:
        # All required vars are present locally — offer to reuse the whole set.
        print("  Found local configuration for this bridge:")
        for var in bridge_vars:
            key = var["key"]
            if key in local_config:
                display = local_config[key]
                # Mask tokens — show first 8 chars only.
                if "token" in key.lower() or "password" in key.lower():
                    display = display[:8] + "..." if len(display) > 8 else "***"
                print(f"    {var['label']}: {display}")
        print()

        try:
            use_local = input("  Use local configuration? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if use_local in ("", "y", "yes"):
            # Start with all local values.
            for var in bridge_vars:
                key = var["key"]
                if key in local_config:
                    env[key] = local_config[key]

            # Discord: offer to create a new channel instead of reusing the existing one.
            if bridge_name == "discord":
                local_channel_id = local_config.get("DISCORD_CHANNEL_ID", "")
                print()
                try:
                    create_new = input(
                        f"  Reuse existing channel ({local_channel_id}), enter a different ID, or create a new one? [reuse/id/create]: "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    sys.exit(0)

                if create_new in ("id", "i"):
                    try:
                        new_id = input("  Channel ID: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        sys.exit(0)
                    if new_id:
                        env["DISCORD_CHANNEL_ID"] = new_id
                    else:
                        print("  No ID entered, keeping existing channel.")
                elif create_new.isdigit():
                    # User pasted the channel ID directly.
                    env["DISCORD_CHANNEL_ID"] = create_new
                elif create_new == "create":
                    # Derive guild ID from the existing channel so we know where to create.
                    bot_token = local_config.get("DISCORD_BOT_TOKEN", "")
                    try:
                        guild_id = _get_discord_guild_id(bot_token, local_channel_id)
                    except RuntimeError as exc:
                        print(f"  Warning: {exc}")
                        try:
                            guild_id = input("  Enter the Discord guild/server ID manually: ").strip()
                        except (EOFError, KeyboardInterrupt):
                            print()
                            sys.exit(0)

                    try:
                        new_channel_name = input(
                            "  New channel name [tether]: "
                        ).strip() or "tether"
                    except (EOFError, KeyboardInterrupt):
                        print()
                        sys.exit(0)

                    print(f"  Creating #{new_channel_name}...")
                    try:
                        new_channel_id = _create_discord_channel(bot_token, guild_id, new_channel_name)
                        env["DISCORD_CHANNEL_ID"] = new_channel_id
                        print(f"  Created channel (ID: {new_channel_id})")
                    except RuntimeError as exc:
                        print(f"  Error creating channel: {exc}", file=sys.stderr)
                        print("  Tip: give the bot 'Manage Channels' permission in your Discord server,")
                        print("       or create a channel manually and use the 'id' option.")
                        print("  Falling back to existing channel.")
                elif create_new not in ("", "reuse", "r"):
                    print(f"  Unrecognised input '{create_new}', keeping existing channel.")
            else:
                # Non-Discord: offer to override per-field.
                print()
                print("  Override any value, or press Enter to keep:")
                for var in bridge_vars:
                    key = var["key"]
                    label = var["label"]
                    current = env.get(key, "")
                    display = current[:8] + "..." if ("token" in key.lower() or "password" in key.lower()) and len(current) > 8 else current
                    try:
                        value = input(f"    {label} [{display}]: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        sys.exit(0)
                    if value:
                        env[key] = value
        else:
            # User declined — fall through to manual entry below.
            local_keys = set()

    if not env:
        # No local config, or user declined — prompt for all values manually.
        for var in bridge_vars:
            key = var["key"]
            label = var["label"]
            required = var.get("required", True)
            hint = "" if required else " (optional, press Enter to skip)"
            try:
                value = input(f"  {label}{hint}: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(0)
            if not value and required:
                print(f"  Error: {label} is required.", file=sys.stderr)
                sys.exit(1)
            if value:
                env[key] = value

    print()

    try:
        result = _post_json(f"/api/setup/bridge/{bridge_name}", {"env": env})
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    print(f"  {result.get('message', 'Done.')}")
    print()
    print(f"Bridge configured. The {bridge_name} bridge is now active on {server_label}.")
