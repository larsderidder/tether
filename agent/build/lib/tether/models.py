"""SQLModel models for database tables and API payloads."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel
from sqlmodel import SQLModel, Field


class SessionState(str, Enum):
    """Lifecycle states for a supervised session."""
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    AWAITING_INPUT = "AWAITING_INPUT"
    INTERRUPTING = "INTERRUPTING"
    ERROR = "ERROR"


# External session models re-exported from agent_sessions
from agent_sessions import (  # noqa: F401, E402
    RunnerType as ExternalRunnerType,
    SessionMessage as ExternalSessionMessage,
    SessionSummary as ExternalSessionSummary,
    SessionDetail as ExternalSessionDetail,
)


class RepoRef(BaseModel):
    """Reference to a repository target (path or URL)."""
    type: str
    value: str


class ErrorDetail(BaseModel):
    """Structured error payload for API responses."""
    code: str
    message: str
    details: dict | None


class ErrorResponse(BaseModel):
    """Envelope for API error responses."""
    error: ErrorDetail


# --- Database Tables (SQLModel with table=True) ---


class Session(SQLModel, table=True):
    """Session table and API model."""
    __tablename__ = "sessions"

    id: str = Field(primary_key=True)
    repo_id: str
    repo_display: str
    repo_ref_type: str
    repo_ref_value: str
    state: SessionState
    name: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    last_activity_at: str
    exit_code: Optional[int] = None
    summary: Optional[str] = None
    runner_header: Optional[str] = None
    runner_type: Optional[str] = None
    runner_session_id: Optional[str] = Field(default=None, unique=True)
    directory: Optional[str] = None
    directory_has_git: bool = False
    workdir_managed: bool = False
    approval_mode: Optional[int] = None  # None = use global default, 0/1/2 = override
    adapter: Optional[str] = None  # Adapter selection (immutable after creation)

    # External agent fields
    external_agent_id: Optional[str] = None
    external_agent_name: Optional[str] = None
    external_agent_type: Optional[str] = None
    external_agent_icon: Optional[str] = None
    external_agent_workspace: Optional[str] = None

    # Platform binding fields
    platform: Optional[str] = None  # e.g., "telegram", "slack", "discord"
    platform_thread_id: Optional[str] = None  # Platform-specific thread ID

    @property
    def repo_ref(self) -> RepoRef:
        """Get repo_ref as a RepoRef object."""
        return RepoRef(type=self.repo_ref_type, value=self.repo_ref_value)

    @repo_ref.setter
    def repo_ref(self, value: RepoRef) -> None:
        """Set repo_ref from a RepoRef object."""
        self.repo_ref_type = value.type
        self.repo_ref_value = value.value


class Message(SQLModel, table=True):
    """Message table and API model."""
    __tablename__ = "messages"

    id: str = Field(primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", ondelete="CASCADE")
    role: str
    content: Optional[str] = None
    created_at: str
    seq: int
