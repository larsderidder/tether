"""Lightweight HTML dashboard served at /.

Read-only status page. No JavaScript framework, no build step. Just
server-rendered HTML with inline styles.
"""

from __future__ import annotations

from html import escape
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from tether.bridges.glue import bridge_manager
from tether.models import SessionState
from tether.store import store

router = APIRouter()

_STATE_LABELS = {
    "CREATED": "created",
    "RUNNING": "running",
    "AWAITING_INPUT": "awaiting input",
    "INTERRUPTING": "stopping",
    "ERROR": "error",
}

_STATE_COLORS = {
    "CREATED": "#6b7280",
    "RUNNING": "#22c55e",
    "AWAITING_INPUT": "#f59e0b",
    "INTERRUPTING": "#f97316",
    "ERROR": "#ef4444",
}


def _format_state(state: str) -> str:
    return _STATE_LABELS.get(state, state.lower())


def _state_dot(state: str) -> str:
    color = _STATE_COLORS.get(state, "#6b7280")
    return f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{color};margin-right:6px;"></span>'


def _time_ago(iso: str | None) -> str:
    """Format an ISO timestamp as a human-readable relative time."""
    if not iso:
        return ""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            m = seconds // 60
            return f"{m}m ago"
        if seconds < 86400:
            h = seconds // 3600
            return f"{h}h ago"
        d = seconds // 86400
        return f"{d}d ago"
    except Exception:
        return ""


def _truncate(text: str | None, length: int = 60) -> str:
    if not text:
        return ""
    if len(text) <= length:
        return text
    return text[:length - 1] + "\u2026"


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the status dashboard."""
    sessions = store.list_sessions()
    sessions.sort(
        key=lambda s: s.last_activity_at or s.created_at,
        reverse=True,
    )

    # Bridge status
    registered = set(bridge_manager.list_bridges())
    bridge_html = ""
    for platform in ("telegram", "slack", "discord"):
        if platform in registered:
            bridge_html += f'<span style="color:#22c55e;">&#x2022;</span> {platform} '
        else:
            bridge_html += f'<span style="color:#6b7280;">&#x2022;</span> <span style="color:#9ca3af;">{platform}</span> '

    if not registered:
        bridge_html = '<span style="color:#9ca3af;">no bridges configured</span>'

    # Session rows
    rows = ""
    for s in sessions:
        state = s.state.value
        name = escape(_truncate(s.name or "Untitled"))
        directory = escape(_truncate(s.directory or "", 40))
        sid = escape(s.id[:12])
        activity = _time_ago(s.last_activity_at or s.created_at)
        platform = escape(s.platform or "")
        adapter = escape(s.runner_type or "")

        rows += f"""<tr>
            <td style="font-family:monospace;font-size:0.85em;color:#6b7280;">{sid}</td>
            <td>{_state_dot(state)}{_format_state(state)}</td>
            <td>{name}</td>
            <td style="font-family:monospace;font-size:0.85em;color:#9ca3af;">{directory}</td>
            <td>{adapter}</td>
            <td>{platform}</td>
            <td style="color:#9ca3af;">{activity}</td>
        </tr>"""

    if not sessions:
        rows = '<tr><td colspan="7" style="color:#9ca3af;text-align:center;padding:2em;">No sessions yet. Use <code>tether attach</code> or <code>tether new</code> to get started.</td></tr>'

    count = len(sessions)
    running = sum(1 for s in sessions if s.state == SessionState.RUNNING)
    awaiting = sum(1 for s in sessions if s.state == SessionState.AWAITING_INPUT)

    summary_parts = [f"{count} session{'s' if count != 1 else ''}"]
    if running:
        summary_parts.append(f"{running} running")
    if awaiting:
        summary_parts.append(f"{awaiting} awaiting input")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tether</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 1.5rem; max-width: 1200px; margin: 0 auto; }}
  h1 {{ font-size: 1.3rem; font-weight: 600; margin-bottom: 0.25rem; }}
  .meta {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 1.5rem; }}
  .meta span {{ margin-right: 1.5rem; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; color: #64748b; font-weight: 500; font-size: 0.8rem;
       text-transform: uppercase; letter-spacing: 0.05em; padding: 0.5rem 0.75rem;
       border-bottom: 1px solid #1e293b; }}
  td {{ padding: 0.6rem 0.75rem; border-bottom: 1px solid #1e293b; font-size: 0.9rem; }}
  tr:hover {{ background: #1e293b; }}
  code {{ font-family: monospace; background: #1e293b; padding: 0.15em 0.4em; border-radius: 3px;
          font-size: 0.85em; }}
  @media (max-width: 768px) {{
    table {{ font-size: 0.8rem; }}
    td, th {{ padding: 0.4rem 0.5rem; }}
    .hide-mobile {{ display: none; }}
  }}
</style>
</head>
<body>
<h1>Tether</h1>
<div class="meta">
  <span>{', '.join(summary_parts)}</span>
  <span>Bridges: {bridge_html}</span>
</div>
<table>
<thead>
<tr>
  <th>ID</th>
  <th>State</th>
  <th>Name</th>
  <th class="hide-mobile">Directory</th>
  <th class="hide-mobile">Adapter</th>
  <th>Platform</th>
  <th>Activity</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>"""

    return HTMLResponse(html)
