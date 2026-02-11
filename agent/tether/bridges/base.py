"""Base interface for messaging platform bridges.

Bridges handle routing agent output to messaging platforms like Telegram, Slack, or Discord.
Each bridge implements platform-specific message formatting and API interactions.
"""

from __future__ import annotations

import asyncio
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from typing import Literal

from tether.settings import settings

_EXTERNAL_PAGE_SIZE = 10
_EXTERNAL_MAX_FETCH = 200
_EXTERNAL_REPLAY_LIMIT = 10
_EXTERNAL_REPLAY_MAX_CHARS = 3500
_ALLOW_ALL_DURATION_S = 30 * 60  # 30 minutes


def _relative_time(iso_str: str) -> str:
    """Convert an ISO timestamp to a short relative time string like '2h ago'."""
    if not iso_str:
        return ""
    try:
        # Handle both Z suffix and +00:00
        ts = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return ""
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except (ValueError, TypeError):
        return ""


class ApprovalRequest(BaseModel):
    """An approval request from an agent to a human."""

    kind: Literal["permission", "choice"] = "permission"
    request_id: str
    title: str
    description: str
    options: list[str]
    timeout_s: int = 300  # Default 5 minutes


class HumanInput(BaseModel):
    """Human input message from a messaging platform."""

    input_id: str
    text: str
    username: str | None = None
    timestamp: str | None = None


class ApprovalResponse(BaseModel):
    """Human response to an approval request."""

    request_id: str
    option_selected: str
    username: str | None = None
    timestamp: str | None = None


class BridgeInterface(ABC):
    """Abstract interface for messaging platform bridges.

    Each platform (Telegram, Slack, Discord) implements this interface to handle
    platform-specific formatting and API calls.

    Provides shared helpers for API access, external session management,
    auto-approval timers, and pagination.
    """

    def __init__(self) -> None:
        # External session cache and pagination
        self._cached_external: list[dict] = []
        self._external_query: str | None = None
        self._external_view: list[dict] = []
        # Auto-approve timers: session_id → expiry timestamp
        self._allow_all_until: dict[str, float] = {}
        self._allow_tool_until: dict[str, dict[str, float]] = {}
        # Directory-scoped auto-approve: normalised_dir → expiry timestamp
        self._allow_dir_until: dict[str, float] = {}
        # Pending permission requests: session_id → request
        self._pending_permissions: dict[str, ApprovalRequest] = {}
        # Debounce error notifications: session_id -> last_sent_timestamp
        self._last_error_status_sent_at: dict[str, float] = {}
        # Auto-approve notification buffer: session_id → list of (tool_name, reason)
        self._auto_approve_buffer: dict[str, list[tuple[str, str]]] = {}
        self._auto_approve_flush_tasks: dict[str, asyncio.Task] = {}
        # Delay before flushing buffered auto-approve notifications (seconds)
        self._auto_approve_flush_delay: float = 1.5

    # ------------------------------------------------------------------
    # API helpers (shared across all bridges)
    # ------------------------------------------------------------------

    def _api_headers(self) -> dict[str, str]:
        """Build auth headers for internal API calls."""
        headers: dict[str, str] = {}
        token = settings.token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _api_url(self, path: str) -> str:
        """Build a localhost API URL."""
        return f"http://localhost:{settings.port()}/api{path}"

    # ------------------------------------------------------------------
    # Formatting helpers (shared across bridges)
    # ------------------------------------------------------------------

    @staticmethod
    def _humanize_key(key: str) -> str:
        """Convert snake_case keys into a human-friendly label.

        Examples:
          output_mode -> Output mode
          session_id  -> Session ID
        """
        # Keep non-snake keys as-is (e.g. "-C", "argc").
        if not key or "_" not in key:
            return key

        acronyms = {
            "id",
            "url",
            "api",
            "sdk",
            "http",
            "https",
            "cli",
            "ui",
            "sse",
            "mcp",
            "json",
        }
        parts = [p for p in key.strip().split("_") if p]
        if not parts:
            return key
        out: list[str] = []
        for i, p in enumerate(parts):
            low = p.lower()
            if low in acronyms:
                out.append(low.upper())
            elif i == 0:
                out.append(low[:1].upper() + low[1:])
            else:
                out.append(low)
        return " ".join(out)

    @staticmethod
    def _humanize_enum_value(value: object) -> str:
        """Humanize enum-ish snake_case values like `files_with_matches`."""
        s = str(value)
        if "_" not in s:
            return s
        # Only touch values that look like enums to avoid mangling paths/commands.
        if not re.fullmatch(r"[a-z0-9_]+", s):
            return s
        parts = [p for p in s.split("_") if p]
        if not parts:
            return s
        out: list[str] = []
        for i, p in enumerate(parts):
            low = p.lower()
            if low == "id":
                out.append("ID")
            elif i == 0:
                out.append(low[:1].upper() + low[1:])
            else:
                out.append(low)
        return " ".join(out)

    def format_tool_input_markdown(
        self,
        raw: str,
        *,
        truncate: int = 400,
        truncate_code: int = 1400,
        max_chars: int = 2000,
    ) -> str:
        """Format tool_input JSON as readable markdown for Slack/Discord.

        This is best-effort formatting; if parsing fails, returns the raw string.
        """
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return str(raw)

        if not isinstance(obj, dict):
            return str(raw)

        path_keys = {"file_path", "path", "notebook_path"}
        code_block_keys = {"command", "old_string", "new_string", "content", "new_source"}

        lines: list[str] = []
        total = 0
        for key, value in obj.items():
            key_s = str(key)
            label = self._humanize_key(key_s)

            if isinstance(value, (dict, list)):
                v = json.dumps(value, ensure_ascii=True)
            else:
                v = self._humanize_enum_value(value)

            limit = truncate_code if key_s in code_block_keys else truncate
            if len(v) > limit:
                v = v[:limit] + "..."

            # Prevent closing the code block early.
            v = v.replace("```", "``\\`")

            if key_s in path_keys:
                part = f"{label}: `{v}`"
            elif key_s in code_block_keys:
                part = f"{label}:\n```\n{v}\n```"
            else:
                part = f"{label}: {v}"

            if total + len(part) > max_chars and lines:
                lines.append("...(truncated)")
                break
            lines.append(part)
            total += len(part) + 1

        return "\n".join(lines).strip()

    async def _create_session_via_api(
        self,
        *,
        directory: str,
        platform: str,
        adapter: str | None = None,
        session_name: str | None = None,
    ) -> dict:
        """Create a new Tether session via the internal API.

        Returns the raw SessionResponse JSON payload.
        """
        import httpx

        payload: dict[str, Any] = {
            "directory": directory,
            "platform": platform,
        }
        if adapter:
            payload["adapter"] = adapter
        if session_name:
            payload["session_name"] = session_name

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self._api_url("/sessions"),
                json=payload,
                headers=self._api_headers(),
                timeout=30.0,
            )
            response.raise_for_status()
        return response.json()

    async def _send_input_or_start_via_api(self, *, session_id: str, text: str) -> None:
        """Send input; if the session is in CREATED, start it with the input as prompt.

        Bridges generally forward human messages as input. Newly-created sessions
        are in CREATED and must be started with /start for the first prompt.
        """
        import httpx

        async def _post_input() -> None:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    self._api_url(f"/sessions/{session_id}/input"),
                    json={"text": text},
                    headers=self._api_headers(),
                    timeout=30.0,
                )
                r.raise_for_status()

        async def _post_start() -> None:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    self._api_url(f"/sessions/{session_id}/start"),
                    json={"prompt": text},
                    headers=self._api_headers(),
                    timeout=30.0,
                )
                r.raise_for_status()

        try:
            await _post_input()
            return
        except httpx.HTTPStatusError as e:
            # CREATED sessions can't accept /input; promote to /start using the same text.
            if e.response.status_code != 409:
                raise
            try:
                data = e.response.json()
            except Exception:
                data = {}
            code = (data.get("error") or {}).get("code")
            if code != "INVALID_STATE":
                raise

        await _post_start()

    async def _resolve_directory_arg(
        self,
        raw: str,
        *,
        base_directory: str | None = None,
    ) -> str:
        """Resolve a directory argument into a full, existing path.

        Rules:
        - If raw looks like a path (contains "/" or starts with "~" or "."), use it as-is.
        - If raw is just a name, resolve relative to:
          - base_directory's parent, if provided
          - otherwise the user's home directory
        - Validate via /api/directories/check and return the normalized path.
        """
        import httpx

        raw = (raw or "").strip()
        if not raw:
            raise ValueError("directory is required")

        looks_like_path = ("/" in raw) or raw.startswith(("~", ".", "/"))
        candidates: list[str] = []

        if looks_like_path:
            candidates.append(raw)
        else:
            if base_directory:
                try:
                    base_parent = Path(base_directory).expanduser().resolve().parent
                    candidates.append(str(base_parent / raw))
                except Exception:
                    # If base_directory is weird, still fall back to home.
                    pass
            candidates.append(str(Path.home() / raw))

        tried: list[str] = []
        for candidate in candidates:
            tried.append(candidate)
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    self._api_url("/directories/check"),
                    params={"path": candidate},
                    headers=self._api_headers(),
                    timeout=10.0,
                )
                r.raise_for_status()
            data = r.json()
            if data.get("exists"):
                return str(data.get("path") or candidate)

        raise ValueError("Directory not found. Tried: " + ", ".join(tried))

    # ------------------------------------------------------------------
    # External session pagination (shared across all bridges)
    # ------------------------------------------------------------------

    def _set_external_view(self, query: str | None) -> None:
        """Filter cached external sessions by directory substring."""
        q = (query or "").strip()
        self._external_query = q or None
        if not self._cached_external:
            self._external_view = []
            return
        if not q:
            self._external_view = list(self._cached_external)
            return
        q_lower = q.lower()
        self._external_view = [
            s
            for s in self._cached_external
            if q_lower in str(s.get("directory", "")).lower()
        ]

    def _format_external_page(
        self, page: int, *, attach_cmd: str = "!attach", list_cmd: str = "!list"
    ) -> tuple[str, int, int]:
        """Format a page of external sessions as text.

        Returns (text, current_page, total_pages).
        """
        sessions = self._external_view or []
        if not sessions:
            if self._external_query:
                return (
                    f"No external sessions match directory search: {self._external_query}\n\n"
                    f"Try a different query, or run {list_cmd} to clear the search.",
                    1,
                    1,
                )
            return (
                "No external sessions found.\n\n"
                f"Start a Claude Code or Codex session first, then use {list_cmd} to see it.",
                1,
                1,
            )

        total = len(sessions)
        total_pages = max(1, (total + _EXTERNAL_PAGE_SIZE - 1) // _EXTERNAL_PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * _EXTERNAL_PAGE_SIZE
        end = min(start + _EXTERNAL_PAGE_SIZE, total)

        title = f"External Sessions (page {page}/{total_pages})"
        if self._external_query:
            title += f" [search: {self._external_query}]"
        lines: list[str] = [f"{title}:\n"]
        for idx in range(start, end):
            s = sessions[idx]
            n = idx + 1
            directory = s.get("directory", "")
            dir_short = directory.rsplit("/", 1)[-1] if directory else "?"
            age = _relative_time(s.get("last_activity", ""))
            # Use last_prompt if available, fallback to first_prompt
            prompt = s.get("last_prompt") or s.get("first_prompt") or ""
            prompt_short = (prompt[:50] + "…") if len(prompt) > 50 else prompt
            # Format: "1. `workspace` • 9m ago"
            header = f"{n}. `{dir_short}`"
            if age:
                header += f" • {age}"
            lines.append(header)
            if prompt_short:
                lines.append(f"   {prompt_short}")

        if (
            not self._external_query
            and len(self._cached_external) == _EXTERNAL_MAX_FETCH
        ):
            lines.append(f"\nShowing up to {_EXTERNAL_MAX_FETCH} sessions (API limit).")
        lines.append(f"\n{attach_cmd} <number> to attach.")
        return "\n".join(lines), page, total_pages

    # ------------------------------------------------------------------
    # Auto-approve logic (shared across all bridges)
    # ------------------------------------------------------------------

    # Tools that require explicit human review and must never be auto-approved.
    _NEVER_AUTO_APPROVE = {"task", "enterplanmode", "exitplanmode"}

    def check_auto_approve(self, session_id: str, tool_name: str) -> str | None:
        """Check if an approval request should be auto-approved.

        Returns the reason string if auto-approved, or None.
        Tools in _NEVER_AUTO_APPROVE always require explicit approval.
        """
        norm = (tool_name or "").strip().lower()
        if any(norm.startswith(prefix) for prefix in self._NEVER_AUTO_APPROVE):
            return None
        now = time.time()
        if now < self._allow_all_until.get(session_id, 0):
            return "Allow All"
        tool_expiry = self._allow_tool_until.get(session_id, {}).get(tool_name, 0)
        if now < tool_expiry:
            return f"Allow {tool_name}"
        # Check directory-scoped timer
        reason = self._check_dir_auto_approve(session_id, now)
        if reason:
            return reason
        return None

    def _check_dir_auto_approve(self, session_id: str, now: float) -> str | None:
        """Check if the session's directory has an active auto-approve timer."""
        if not self._allow_dir_until:
            return None
        from tether.store import store

        session = store.get_session(session_id)
        if not session or not session.directory:
            return None
        sess_dir = os.path.normpath(session.directory)
        for allowed_dir, expiry in self._allow_dir_until.items():
            if now >= expiry:
                continue
            if sess_dir == allowed_dir or sess_dir.startswith(allowed_dir + os.sep):
                short = os.path.basename(allowed_dir) or allowed_dir
                return f"Allow dir {short}"
        return None

    def set_allow_all(self, session_id: str) -> None:
        """Enable auto-approve for all tools for 30 minutes."""
        self._allow_all_until[session_id] = time.time() + _ALLOW_ALL_DURATION_S

    def set_allow_tool(self, session_id: str, tool_name: str) -> None:
        """Enable auto-approve for a specific tool for 30 minutes."""
        self._allow_tool_until.setdefault(session_id, {})[tool_name] = (
            time.time() + _ALLOW_ALL_DURATION_S
        )

    def set_allow_directory(self, directory: str) -> None:
        """Enable auto-approve for all sessions in *directory* for 30 minutes."""
        norm = os.path.normpath(directory)
        self._allow_dir_until[norm] = time.time() + _ALLOW_ALL_DURATION_S

    async def _auto_approve(
        self, session_id: str, request: ApprovalRequest, *, reason: str = "Allow All"
    ) -> None:
        """Silently approve a permission request via the API."""
        import httpx
        import structlog

        logger = structlog.get_logger(__name__)
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    self._api_url(f"/sessions/{session_id}/permission"),
                    json={
                        "request_id": request.request_id,
                        "allow": True,
                        "message": f"Auto-approved ({reason} active)",
                    },
                    headers=self._api_headers(),
                    timeout=10.0,
                )
            logger.info(
                "Auto-approved via %s",
                reason,
                session_id=session_id,
                request_id=request.request_id,
            )
        except Exception:
            logger.exception("Failed to auto-approve", request_id=request.request_id)

    def buffer_auto_approve_notification(
        self, session_id: str, tool_name: str, reason: str
    ) -> None:
        """Buffer an auto-approve notification for batched delivery.

        Instead of sending one Telegram/Slack/Discord message per auto-approved
        tool, this collects them and flushes after a short delay so rapid-fire
        approvals collapse into a single message.
        """
        buf = self._auto_approve_buffer.setdefault(session_id, [])
        buf.append((tool_name, reason))

        # Cancel existing flush timer and start a new one
        existing = self._auto_approve_flush_tasks.pop(session_id, None)
        if existing:
            existing.cancel()

        self._auto_approve_flush_tasks[session_id] = asyncio.create_task(
            self._flush_auto_approve_after_delay(session_id)
        )

    async def _flush_auto_approve_after_delay(self, session_id: str) -> None:
        """Wait then flush buffered auto-approve notifications."""
        try:
            await asyncio.sleep(self._auto_approve_flush_delay)
        except asyncio.CancelledError:
            return
        self._auto_approve_flush_tasks.pop(session_id, None)
        items = self._auto_approve_buffer.pop(session_id, [])
        if items:
            await self.send_auto_approve_batch(session_id, items)

    async def send_auto_approve_batch(
        self, session_id: str, items: list[tuple[str, str]]
    ) -> None:
        """Send a batched auto-approve notification.

        Override in subclasses to format for the specific platform.
        Default implementation calls ``on_output`` with a plain text summary.
        """
        if len(items) == 1:
            tool_name, reason = items[0]
            text = f"✅ {tool_name} — auto-approved ({reason})"
        else:
            lines = [f"✅ Auto-approved {len(items)} tools:"]
            for tool_name, reason in items:
                lines.append(f"  • {tool_name}")
            lines.append(f"({items[0][1]})")
            text = "\n".join(lines)
        await self.on_output(session_id, text)

    # ------------------------------------------------------------------
    # Usage helper
    # ------------------------------------------------------------------

    async def _fetch_usage(self, session_id: str) -> dict:
        """Fetch token/cost usage for a session from the API."""
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(
                self._api_url(f"/sessions/{session_id}/usage"),
                headers=self._api_headers(),
                timeout=10.0,
            )
            response.raise_for_status()
        return response.json()

    def _format_usage_text(self, usage: dict) -> str:
        """Format usage dict as plain text."""
        input_t = usage.get("input_tokens", 0)
        output_t = usage.get("output_tokens", 0)
        cost = usage.get("total_cost_usd", 0.0)

        lines = [
            "Session Usage",
            "",
            f"Input tokens:  {input_t:,}",
            f"Output tokens: {output_t:,}",
            f"Total tokens:  {input_t + output_t:,}",
        ]
        if cost > 0:
            lines.append(f"Cost: ${cost:.4f}")
        else:
            lines.append("Cost: not tracked")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Shared constants
    # ------------------------------------------------------------------

    _STATE_EMOJI: dict[str, str] = {
        "CREATED": "🆕",
        "RUNNING": "🔄",
        "AWAITING_INPUT": "📝",
        "INTERRUPTING": "⏳",
        "ERROR": "❌",
    }

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _agent_to_adapter(raw: str) -> str | None:
        """Map a user-friendly agent name to an adapter name."""
        key = (raw or "").strip().lower()
        if not key:
            return None
        aliases = {
            "claude": "claude_auto",
            "codex": "codex_sdk_sidecar",
        }
        if key in aliases:
            return aliases[key]
        # Allow explicit adapter names.
        if key in {"claude_auto", "claude_subprocess", "claude_api", "codex_sdk_sidecar"}:
            return key
        return None

    @staticmethod
    def _adapter_label(adapter: str | None) -> str | None:
        """Map an adapter name to a user-friendly label, or None to omit."""
        _labels: dict[str, str] = {
            "claude_auto": "Claude",
            "claude_subprocess": "Claude",
            "claude_api": "Claude API",
            "codex_sdk_sidecar": "Codex",
        }
        if not adapter:
            return None
        return _labels.get(adapter, adapter)

    # ------------------------------------------------------------------
    # Optional lifecycle hooks (override as needed)
    # ------------------------------------------------------------------

    async def on_typing(self, session_id: str) -> None:
        """Show a typing indicator. Override if platform supports it."""

    async def on_typing_stopped(self, session_id: str) -> None:
        """Stop the typing indicator. Override if platform supports it."""

    def set_pending_permission(self, session_id: str, request: ApprovalRequest) -> None:
        """Track a pending permission request for a session."""
        self._pending_permissions[session_id] = request

    def get_pending_permission(self, session_id: str) -> ApprovalRequest | None:
        """Get the pending permission request for a session, if any."""
        return self._pending_permissions.get(session_id)

    def clear_pending_permission(self, session_id: str) -> None:
        """Clear the pending permission request for a session."""
        self._pending_permissions.pop(session_id, None)

    async def _respond_to_permission(
        self,
        session_id: str,
        request_id: str,
        *,
        allow: bool,
        message: str | None = None,
    ) -> bool:
        """Send a permission response via the API.

        Returns True on success, False on error.
        """
        import httpx
        import structlog

        logger = structlog.get_logger(__name__)
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    self._api_url(f"/sessions/{session_id}/permission"),
                    json={
                        "request_id": request_id,
                        "allow": allow,
                        "message": message
                        or ("Approved" if allow else "User denied permission"),
                    },
                    headers=self._api_headers(),
                    timeout=10.0,
                )
                r.raise_for_status()
            self.clear_pending_permission(session_id)
            return True
        except Exception:
            logger.exception(
                "Failed to respond to permission",
                session_id=session_id,
                request_id=request_id,
            )
            return False

    def parse_approval_text(self, text: str) -> dict | None:
        """Parse a text message as an approval response.

        Returns a dict with keys: allow (bool), reason (str|None), timer (str|None)
        or None if the text is not an approval command.

        Recognized patterns:
          allow / yes / approve          → allow
          deny / no / reject             → deny without reason
          deny: <reason>                 → deny with reason
          deny <reason>                  → deny with reason (if >1 word after deny)
          allow all                      → allow + timer "all"
          allow <tool>                   → allow + timer tool name
        """
        stripped = text.strip()
        lower = stripped.lower()

        # Common synonyms (esp. for "Task"/plan-style prompts)
        if lower in ("proceed", "continue", "start", "go", "ok", "okay"):
            return {"allow": True, "reason": None, "timer": None}
        if lower in ("cancel", "stop", "abort"):
            return {"allow": False, "reason": None, "timer": None}

        # "allow all" → approve with allow-all timer
        if lower == "allow all":
            return {"allow": True, "reason": None, "timer": "all"}

        # "allow dir" → approve with directory-scoped timer
        if lower == "allow dir":
            return {"allow": True, "reason": None, "timer": "dir"}

        # "allow <tool>" → approve with tool timer (but not bare "allow")
        if lower.startswith("allow ") and lower != "allow all":
            rest = stripped[6:].strip()
            if rest:
                return {"allow": True, "reason": None, "timer": rest}

        # Bare allow/approve/yes
        if lower in ("allow", "approve", "yes"):
            return {"allow": True, "reason": None, "timer": None}

        # "deny: reason" or "deny reason" (multi-word)
        if (
            lower.startswith("deny:")
            or lower.startswith("reject:")
            or lower.startswith("no:")
        ):
            sep = stripped.index(":")
            reason = stripped[sep + 1 :].strip()
            return {"allow": False, "reason": reason or None, "timer": None}

        if lower.startswith("deny ") or lower.startswith("reject "):
            first_space = stripped.index(" ")
            reason = stripped[first_space + 1 :].strip()
            if reason:
                return {"allow": False, "reason": reason, "timer": None}

        # Bare deny/reject/no
        if lower in ("deny", "reject", "no"):
            return {"allow": False, "reason": None, "timer": None}

        return None

    def parse_choice_text(self, session_id: str, text: str) -> str | None:
        """Parse a text message as a choice selection for a pending choice request.

        Supports:
        - `1`..`N` selecting the corresponding option (1-indexed)
        - matching an option label (case-insensitive)
        """
        pending = self.get_pending_permission(session_id)
        if not pending or pending.kind != "choice":
            return None

        stripped = text.strip()
        if not stripped:
            return None

        # Numeric selection (1-indexed)
        if stripped.isdigit():
            idx = int(stripped) - 1
            if 0 <= idx < len(pending.options):
                return pending.options[idx]
            return None

        # Label match (case-insensitive)
        lowered = stripped.casefold()
        for opt in pending.options:
            if opt.casefold() == lowered:
                return opt
        return None

    async def on_session_removed(self, session_id: str) -> None:
        """Clean up when a session is deleted."""
        self._allow_all_until.pop(session_id, None)
        self._allow_tool_until.pop(session_id, None)
        self._pending_permissions.pop(session_id, None)
        self._last_error_status_sent_at.pop(session_id, None)

    def _should_send_error_status(self, session_id: str) -> bool:
        """Return True if an 'error' status notification should be sent now.

        Used by bridges to debounce repeated error events/status changes.
        """
        debounce_s = max(0, int(settings.bridge_error_debounce_seconds() or 0))
        if debounce_s == 0:
            return True

        now_ts = time.time()
        last = self._last_error_status_sent_at.get(session_id)
        if last is not None and (now_ts - last) < debounce_s:
            return False

        self._last_error_status_sent_at[session_id] = now_ts
        return True

    # ------------------------------------------------------------------
    # Required interface methods
    # ------------------------------------------------------------------

    @abstractmethod
    async def on_output(
        self, session_id: str, text: str, metadata: dict | None = None
    ) -> None:
        """Handle agent output text."""
        pass

    @abstractmethod
    async def on_approval_request(
        self, session_id: str, request: ApprovalRequest
    ) -> None:
        """Handle an approval request."""
        pass

    @abstractmethod
    async def on_status_change(
        self, session_id: str, status: str, metadata: dict | None = None
    ) -> None:
        """Handle agent status change."""
        pass

    @abstractmethod
    async def create_thread(self, session_id: str, session_name: str) -> dict:
        """Create a messaging thread for a new session."""
        pass
