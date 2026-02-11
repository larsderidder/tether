"""Pydantic request/response models for API endpoints."""

from __future__ import annotations

from typing import Literal, TYPE_CHECKING

from pydantic import BaseModel, Field

from tether.models import (
    ExternalRunnerType,
    ExternalSessionMessage,
    SessionState,
)

if TYPE_CHECKING:
    from tether.models import Session
    from tether.store import SessionStore


# --- Request Models ---


class CreateSessionRequest(BaseModel):
    """Request body for creating a new session."""

    repo_id: str | None = None
    directory: str | None = None
    base_ref: str | None = None
    adapter: str | None = None
    # External agent fields
    agent_name: str | None = None
    agent_type: str | None = None
    agent_icon: str | None = None
    agent_workspace: str | None = None
    platform: str | None = None  # "telegram", "slack", "discord"
    session_name: str | None = None


class StartSessionRequest(BaseModel):
    """Request body for starting a session.

    approval_choice values:
        0 = Interactive (ask for permissions via UI)
        1 = Auto-approve edits only
        2 = Full auto-approve (bypass all permissions)
    """

    prompt: str = ""
    approval_choice: Literal[0, 1, 2] = 2


class RenameSessionRequest(BaseModel):
    """Request body for renaming a session."""

    name: str = Field(..., min_length=1, max_length=80)


class InputRequest(BaseModel):
    """Request body for sending input to a session."""

    text: str = Field(..., min_length=1)


class PermissionResponseRequest(BaseModel):
    """Request body for responding to a permission request."""

    request_id: str = Field(..., min_length=1)
    allow: bool
    message: str | None = None
    updated_input: dict | None = None


class AgentEventRequest(BaseModel):
    """Request body for pushing an event from an external agent."""

    type: Literal["output", "status", "error", "permission_request"]
    data: dict


class AttachSessionRequest(BaseModel):
    """Request body for attaching to an external session."""

    external_id: str
    runner_type: ExternalRunnerType
    directory: str


class UpdateApprovalModeRequest(BaseModel):
    """Request body for updating session approval mode.

    approval_mode values:
        None = Use global default
        0 = Interactive (ask for permissions via UI)
        1 = Auto-approve edits only
        2 = Full auto-approve (bypass all permissions)
    """

    approval_mode: Literal[0, 1, 2] | None = None


# --- Response Models ---


class SessionResponse(BaseModel):
    """Session data returned by API endpoints."""

    id: str
    state: SessionState
    name: str | None
    created_at: str
    started_at: str | None
    ended_at: str | None
    last_activity_at: str
    exit_code: int | None
    summary: str | None
    runner_header: str | None
    runner_type: str | None
    runner_session_id: str | None
    directory: str | None
    directory_has_git: bool
    message_count: int
    has_pending_permission: bool
    approval_mode: int | None  # None = use global default, 0/1/2 = override
    adapter: str | None  # Adapter configured for this session
    # External agent fields
    external_agent_name: str | None = None
    external_agent_type: str | None = None
    external_agent_icon: str | None = None
    platform: str | None = None
    platform_thread_id: str | None = None

    @classmethod
    def from_session(cls, session: Session, store: SessionStore) -> SessionResponse:
        """Create a SessionResponse from a Session model and store."""
        return cls(
            id=session.id,
            state=session.state,
            name=session.name,
            created_at=session.created_at,
            started_at=session.started_at,
            ended_at=session.ended_at,
            last_activity_at=session.last_activity_at,
            exit_code=session.exit_code,
            summary=session.summary,
            runner_header=getattr(session, "runner_header", None),
            runner_type=getattr(session, "runner_type", None),
            runner_session_id=store.get_runner_session_id(session.id),
            directory=session.directory,
            directory_has_git=session.directory_has_git,
            message_count=store.get_message_count(session.id),
            has_pending_permission=len(store.get_all_pending_permissions(session.id)) > 0,
            approval_mode=session.approval_mode,
            adapter=session.adapter,
            external_agent_name=session.external_agent_name,
            external_agent_type=session.external_agent_type,
            external_agent_icon=session.external_agent_icon,
            platform=session.platform,
            platform_thread_id=session.platform_thread_id,
        )


class OkResponse(BaseModel):
    """Simple success response."""

    ok: bool = True


class DiffFile(BaseModel):
    """A file in a git diff."""

    path: str
    hunks: int
    patch: str


class DiffResponse(BaseModel):
    """Git diff response."""

    diff: str
    files: list[DiffFile]


class DirectoryCheckResponse(BaseModel):
    """Response for directory check endpoint."""

    path: str
    exists: bool
    is_git: bool


class SyncResult(BaseModel):
    """Result of syncing an external session."""

    synced: int
    total: int


class HealthResponse(BaseModel):
    """Health check response."""

    ok: bool
    version: str
    protocol: int


# --- External Session Response Models ---


class ExternalSessionSummaryResponse(BaseModel):
    """External session summary for API responses."""

    id: str
    runner_type: ExternalRunnerType
    directory: str
    first_prompt: str | None
    last_prompt: str | None
    last_activity: str
    message_count: int
    is_running: bool


class ExternalSessionDetailResponse(ExternalSessionSummaryResponse):
    """External session detail with messages for API responses."""

    messages: list[ExternalSessionMessage] = []
