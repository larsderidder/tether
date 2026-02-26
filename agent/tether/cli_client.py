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
        timeout=10.0,
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


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_open() -> None:
    """Open the web UI in the default browser."""
    url = _base_url()
    print(f"Opening {url}")
    webbrowser.open(url)


def cmd_status() -> None:
    """Print server health and session summary."""
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
            f"{count} {_format_state(state)}" for state, count in sorted(by_state.items())
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


def cmd_list_external(directory: str | None, runner_type: str | None) -> None:
    """List discoverable external sessions."""
    params: dict[str, str] = {}
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
                s for s in ext_sessions
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
    directory: str,
    adapter: str | None = None,
    prompt: str | None = None,
    platform: str | None = None,
) -> None:
    """Create a new session and optionally start it with a prompt."""
    body: dict = {"directory": directory}
    if adapter:
        body["adapter"] = adapter
    if platform:
        body["platform"] = platform

    try:
        with _client() as c:
            resp = c.post("/api/sessions", json=body)
            if resp.status_code == 422:
                # Try to surface a helpful message for missing adapter
                try:
                    msg = (resp.json().get("error") or {}).get("message", "")
                except Exception:
                    msg = ""
                if "no default adapter" in msg.lower() or "not configured" in msg.lower():
                    print(
                        "Error: No adapter specified and TETHER_DEFAULT_AGENT_ADAPTER is not set.\n"
                        "Use -a to specify one: tether new . -a claude_auto\n"
                        "Or set a default: echo 'TETHER_DEFAULT_AGENT_ADAPTER=claude_auto'"
                        " >> ~/.config/tether/config.env",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            _check_response(resp)
            session = resp.json()

        if prompt:
            with _client() as c:
                resp = c.post(
                    f"/api/sessions/{session['id']}/start",
                    json={"prompt": prompt},
                )
                _check_response(resp)
                session = resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _handle_connection_error()
        return

    print(f"Created session {session['id']}")
    print(f"  Directory: {session.get('directory') or '?'}")
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

    print(f"Sent to {_short_id(session_id)} ({_format_state(session.get('state', '?'))})")


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
        print(f"Synced {synced} new message{'s' if synced != 1 else ''} ({total} total)")


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
            print(f"Error: server returned {resp.status}: {body[:200]}", file=sys.stderr)
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
        items = [
            s for s in items
            if s.get("directory", "").rstrip("/") == norm_dir
        ]
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
        sorted_items.extend(sorted(group, key=lambda s: s.get("last_activity_at") or "", reverse=True))
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
