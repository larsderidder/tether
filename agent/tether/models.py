"""Pydantic models for API payloads and session metadata."""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel


class SessionState(str, Enum):
    """Lifecycle states for a supervised session."""
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    AWAITING_INPUT = "AWAITING_INPUT"
    INTERRUPTING = "INTERRUPTING"
    ERROR = "ERROR"


class RepoRef(BaseModel):
    """Reference to a repository target (path or URL)."""
    type: str
    value: str


class Session(BaseModel):
    """Server-side session metadata exposed over the API."""
    id: str
    repo_id: str
    repo_display: str
    repo_ref: RepoRef
    state: SessionState
    name: str | None = None
    created_at: str
    started_at: str | None
    ended_at: str | None
    last_activity_at: str
    exit_code: int | None
    summary: str | None
    runner_header: str | None = None
    runner_type: str | None = None
    directory: str | None = None
    directory_has_git: bool = False


class ErrorDetail(BaseModel):
    """Structured error payload for API responses."""
    code: str
    message: str
    details: dict | None


class ErrorResponse(BaseModel):
    """Envelope for API error responses."""
    error: ErrorDetail


class Message(BaseModel):
    """Conversation message for Claude runner history."""
    id: str
    session_id: str
    role: str  # "user", "assistant"
    content: str  # JSON-encoded content blocks
    seq: int
    created_at: str
