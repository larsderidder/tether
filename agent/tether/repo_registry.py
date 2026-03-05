"""Repo registry for tracking shared git clones used as worktree bases.

Maps repository URLs to shared local clones stored under {data_dir}/repos/.
The first session for a URL performs a full clone; subsequent sessions create
git worktrees from that clone via workspace.py.

Storage layout::

    {data_dir}/
      repos/
        registry.json
        a1b2c3d4/        # shared clone keyed by URL hash
        ...

Thread safety: all reads and writes hold a threading.Lock.  The agent runs in
a single process (uvicorn), so a threading lock is sufficient.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# URL normalisation helpers
# ---------------------------------------------------------------------------


def normalize_repo_url(url: str) -> str:
    """Return a canonical form of a git remote URL.

    The following transformations are applied so that different URL styles
    for the same repository resolve to the same key:

    - Strip leading/trailing whitespace.
    - Convert SSH SCP syntax ``git@host:path`` to ``host/path``.
    - Strip the ``https://``, ``http://``, ``git://``, and ``ssh://`` schemes.
    - Lowercase the hostname portion.
    - Strip a trailing ``.git`` suffix.
    - Strip a trailing ``/``.
    """
    url = url.strip()

    # Convert SCP-style SSH: git@github.com:user/repo -> github.com/user/repo
    scp_match = re.match(r"^(?:[^@]+@)([^:]+):(.+)$", url)
    if scp_match:
        host = scp_match.group(1).lower()
        path = scp_match.group(2)
        url = f"{host}/{path}"
    else:
        # Strip scheme
        url = re.sub(r"^(?:https?|git|ssh)://", "", url, count=1)
        # Strip optional user@ prefix after scheme removal
        url = re.sub(r"^[^@]+@", "", url)
        # Lowercase only the host part (everything up to the first /)
        parts = url.split("/", 1)
        url = parts[0].lower() + ("/" + parts[1] if len(parts) > 1 else "")

    # Strip trailing .git and trailing slash
    if url.endswith(".git"):
        url = url[:-4]
    url = url.rstrip("/")

    return url


def repo_url_hash(url: str) -> str:
    """Return the first 8 hex characters of the SHA-256 of the normalised URL."""
    canonical = normalize_repo_url(url)
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class RepoEntry(BaseModel):
    """Metadata for a registered shared clone."""

    url: str
    path: str
    created_at: str
    worktree_count: int = 0
    last_used_at: str


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class RepoRegistry:
    """Thread-safe registry mapping git URLs to shared local clones."""

    def __init__(self, data_dir: str) -> None:
        self._repos_dir = Path(data_dir) / "repos"
        self._registry_file = self._repos_dir / "registry.json"
        self._lock = threading.Lock()
        self._repos_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, url: str) -> RepoEntry | None:
        """Look up a repo by URL. Returns None if not registered."""
        with self._lock:
            data = self._load()
            key = repo_url_hash(url)
            raw = data.get(key)
            if raw is None:
                return None
            return RepoEntry(**raw)

    def register(self, url: str, path: str) -> RepoEntry:
        """Register a new shared clone.

        Raises ValueError if a clone for this URL is already registered.
        """
        with self._lock:
            data = self._load()
            key = repo_url_hash(url)
            if key in data:
                raise ValueError(
                    f"Repo already registered for URL '{url}' (key {key})"
                )
            now = _utcnow()
            entry = RepoEntry(
                url=url,
                path=str(path),
                created_at=now,
                worktree_count=0,
                last_used_at=now,
            )
            data[key] = entry.model_dump()
            self._save(data)
            return entry

    def increment_worktrees(self, url: str) -> None:
        """Increment the worktree count for a registered repo."""
        with self._lock:
            data = self._load()
            key = repo_url_hash(url)
            if key not in data:
                raise KeyError(f"No repo registered for URL '{url}'")
            data[key]["worktree_count"] = data[key].get("worktree_count", 0) + 1
            data[key]["last_used_at"] = _utcnow()
            self._save(data)

    def decrement_worktrees(self, url: str) -> None:
        """Decrement the worktree count for a registered repo.

        The count is clamped at zero; it will not go negative.
        """
        with self._lock:
            data = self._load()
            key = repo_url_hash(url)
            if key not in data:
                raise KeyError(f"No repo registered for URL '{url}'")
            current = data[key].get("worktree_count", 0)
            data[key]["worktree_count"] = max(0, current - 1)
            data[key]["last_used_at"] = _utcnow()
            self._save(data)

    def decrement_worktrees_by_path(self, path: str) -> None:
        """Decrement the worktree count by shared clone path.

        Useful when the caller knows the on-disk path but not the original URL.
        No-op if no repo is registered at that path.
        """
        with self._lock:
            data = self._load()
            resolved = str(Path(path).resolve())
            for key, raw in data.items():
                if str(Path(raw["path"]).resolve()) == resolved:
                    current = raw.get("worktree_count", 0)
                    data[key]["worktree_count"] = max(0, current - 1)
                    data[key]["last_used_at"] = _utcnow()
                    self._save(data)
                    return

    def remove(self, url: str) -> None:
        """Unregister a repo and remove its shared clone directory from disk."""
        with self._lock:
            data = self._load()
            key = repo_url_hash(url)
            if key not in data:
                return
            clone_path = Path(data[key]["path"])
            del data[key]
            self._save(data)

        # Remove from disk outside the lock to avoid blocking other operations.
        if clone_path.exists():
            shutil.rmtree(str(clone_path), ignore_errors=True)

    def list_repos(self) -> list[RepoEntry]:
        """Return all registered repos, sorted by last_used_at descending."""
        with self._lock:
            data = self._load()
        entries = [RepoEntry(**raw) for raw in data.values()]
        entries.sort(key=lambda e: e.last_used_at, reverse=True)
        return entries

    # ------------------------------------------------------------------
    # Internal I/O
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, dict]:
        """Read registry.json and return the inner 'repos' dict."""
        if not self._registry_file.exists():
            return {}
        try:
            text = self._registry_file.read_text(encoding="utf-8")
            payload = json.loads(text)
            return payload.get("repos", {})
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        """Atomically write the registry to disk."""
        tmp = self._registry_file.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps({"repos": data}, indent=2), encoding="utf-8"
            )
            tmp.replace(self._registry_file)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()
