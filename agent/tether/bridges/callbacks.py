"""Tether-specific callbacks layered on the published bridge interface."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agent_tether import BridgeCallbacks as BaseBridgeCallbacks


@dataclass
class BridgeCallbacks(BaseBridgeCallbacks):
    """Keep Git commands independent of unpublished agent-tether additions."""

    git_status: Callable[..., Awaitable[dict]] | None = None
    git_commit: Callable[..., Awaitable[dict]] | None = None
    git_push: Callable[..., Awaitable[dict]] | None = None
    git_pr: Callable[..., Awaitable[dict]] | None = None
    git_diff: Callable[..., Awaitable[dict]] | None = None
    git_log: Callable[..., Awaitable[dict]] | None = None
