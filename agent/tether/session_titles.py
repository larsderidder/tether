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

_LEADING_TASK_PREFIXES = (
    "continue with ",
    "clean up ",
    "set up ",
    "fix ",
    "debug ",
    "investigate ",
    "review ",
    "rename ",
    "update ",
    "refactor ",
    "implement ",
    "add ",
    "create ",
    "write ",
    "build ",
    "make ",
    "support ",
    "handle ",
    "improve ",
    "document ",
    "summarize ",
    "test ",
    "verify ",
    "continue ",
    "finish ",
    "ship ",
    "wire up ",
    "remove ",
)

_LEADING_ARTICLES = (
    "the ",
    "a ",
    "an ",
    "this ",
    "that ",
    "these ",
    "those ",
    "my ",
    "our ",
    "your ",
)

_GENERIC_RENAME_TARGETS = (
    "thread ",
    "session ",
    "chat ",
    "conversation ",
)


def _normalize_whitespace(value: str | None) -> str:
    return " ".join((value or "").split())


def _strip_prompt_wrappers(text: str) -> str:
    cleaned = text.strip().strip("\"'`")
    while cleaned:
        lowered = cleaned.lower()
        matched = False
        for prefix in _COMMON_PROMPT_PREFIXES:
            if lowered.startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()
                matched = True
                break
        if not matched:
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


def _strip_leading_task_prefix(text: str) -> tuple[str, str | None]:
    lowered = text.lower()
    for prefix in _LEADING_TASK_PREFIXES:
        if lowered.startswith(prefix):
            return text[len(prefix) :].strip(), prefix.strip()
    return text, None


def _strip_leading_articles(text: str) -> str:
    cleaned = text.strip()
    while cleaned:
        lowered = cleaned.lower()
        matched = False
        for prefix in _LEADING_ARTICLES:
            if lowered.startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()
                matched = True
                break
        if not matched:
            break
    return cleaned


def _strip_generic_rename_target(text: str, *, action: str | None) -> str:
    if action != "rename":
        return text
    lowered = text.lower()
    for prefix in _GENERIC_RENAME_TARGETS:
        if lowered.startswith(prefix):
            candidate = text[len(prefix) :].strip()
            if candidate:
                return candidate
    return text


def summarize_prompt_for_session(prompt: str) -> str | None:
    """Extract a short subject-focused summary from the user's prompt."""
    source = _first_meaningful_line(prompt or "")
    if not source:
        return None

    summary, action = _strip_leading_task_prefix(source)
    summary = _strip_leading_articles(summary)
    summary = _strip_generic_rename_target(summary, action=action)
    summary = _normalize_whitespace(summary)
    return summary or source


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
    """Format ``repo-slug: short session summary`` from a user prompt."""
    source = summarize_prompt_for_session(prompt)
    if not source:
        return None

    repo_slug = project_slug_for_session(session)
    prefix = f"{repo_slug}: "
    budget = max_len - len(prefix)
    if budget <= 0:
        return _truncate_nicely(repo_slug, max_len)

    return prefix + _truncate_nicely(source, budget)
