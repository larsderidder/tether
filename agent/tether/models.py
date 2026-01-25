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


class ExternalRunnerType(str, Enum):
    """Types of external session sources."""
    CLAUDE_CODE = "claude_code"
    CODEX_CLI = "codex_cli"


class ExternalSessionMessage(BaseModel):
    """Normalized message from external session history."""
    role: str  # "user" or "assistant"
    content: str
    thinking: str | None = None  # Thinking content for assistant messages
    timestamp: str | None = None


class ExternalSessionSummary(BaseModel):
    """Summary info for an external session (for list view)."""
    id: str  # External session ID (UUID)
    runner_type: ExternalRunnerType
    directory: str
    first_prompt: str | None = None
    last_activity: str
    message_count: int
    is_running: bool


class ExternalSessionDetail(ExternalSessionSummary):
    """Full external session with message history."""
    messages: list[ExternalSessionMessage] = []


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
    runner_session_id: str | None = None  # External Claude/Codex session ID
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
