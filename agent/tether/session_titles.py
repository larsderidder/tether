"""Helpers for deriving stable session and bridge thread titles."""

from __future__ import annotations

import re
from pathlib import Path

from agent_tether.thread_naming import format_thread_name

from tether.models import Session

DEFAULT_SESSION_NAME = "New session"
MAX_SESSION_NAME = 80

_COMMON_PROMPT_PREFIXES = (
    "please ",
    "pls ",
    "let's ",
    "lets ",
    "can you ",
    "could you ",
    "help me ",
)


def _normalize_whitespace(value: str | None) -> str:
    return " ".join((value or "").split())


def _strip_prompt_wrappers(text: str) -> str:
    cleaned = text.strip().strip("\"'`")
    lowered = cleaned.lower()
    for prefix in _COMMON_PROMPT_PREFIXES:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned


def _first_meaningful_line(text: str) -> str:
    for raw_line in text.splitlines():
        line = _normalize_whitespace(raw_line)
        if not line:
            continue
        if line.startswith("```"):
            continue
        return _strip_prompt_wrappers(line)
    return _strip_prompt_wrappers(text)


def _truncate_nicely(text: str, max_len: int) -> str:
    cleaned = _normalize_whitespace(text)
    if max_len <= 0:
        return ""
    if len(cleaned) <= max_len:
        return cleaned
    if max_len <= 3:
        return cleaned[:max_len]
    cutoff = cleaned.rfind(" ", 0, max_len - 3)
    if cutoff <= 0:
        cutoff = max_len - 3
    return cleaned[:cutoff].rstrip(" ,.:;/-") + "..."


def project_slug_for_session(session: Session) -> str:
    """Build a lowercase slug from the session directory or repo reference."""
    candidates = (
        session.directory,
        session.repo_display,
        session.repo_ref_value,
        session.repo_id,
    )
    for raw in candidates:
        value = _normalize_whitespace(raw)
        if not value:
            continue
        name = Path(value).name or value
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if slug:
            return slug
    return "session"


def is_auto_session_name(session: Session, name: str | None = None) -> bool:
    """Return True when the current name is a placeholder or generic thread label."""
    cleaned = _normalize_whitespace(name if name is not None else session.name)
    if not cleaned:
        return True
    if cleaned.casefold() == DEFAULT_SESSION_NAME.casefold():
        return True

    generic = format_thread_name(
        directory=session.directory,
        runner_type=session.runner_type,
        adapter=session.adapter,
        max_len=64,
    )
    if generic and (
        cleaned == generic or bool(re.fullmatch(rf"{re.escape(generic)} \d+", cleaned))
    ):
        return True
    return False


def build_auto_session_name(
    session: Session,
    prompt: str,
    *,
    max_len: int = MAX_SESSION_NAME,
) -> str | None:
    """Format ``repo-slug: short session title`` from a user prompt."""
    source = _first_meaningful_line(prompt or "")
    if not source:
        return None

    repo_slug = project_slug_for_session(session)
    prefix = f"{repo_slug}: "
    budget = max_len - len(prefix)
    if budget <= 0:
        return _truncate_nicely(repo_slug, max_len)

    return prefix + _truncate_nicely(source, budget)
