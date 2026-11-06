"""Advertise runner permission and approval capabilities."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from tether.adapter_names import normalize_adapter_name

ApprovalModeUpdate = Literal["live", "next_turn", "unsupported"]
ToolApprovalTiming = Literal[
    "pre_execution", "post_start_notification", "not_applicable"
]


@dataclass(frozen=True)
class AdapterPermissionCapabilities:
    """Permission capabilities that Tether can honestly provide for an adapter."""

    adapter: str
    approval_modes_supported: bool
    initial_approval_policy_supported: bool
    approval_mode_updates: ApprovalModeUpdate
    ordinary_tool_approvals: bool
    ordinary_tool_approval_timing: ToolApprovalTiming
    extension_ui_permissions: bool
    approval_mode_runner_update: bool = False
    unsupported_approval_mode_message: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        """Return the public API representation without internal hints."""
        data = asdict(self)
        data.pop("approval_mode_runner_update", None)
        return data


_PI_UNSUPPORTED_MESSAGE = (
    "The Pi RPC adapter cannot pause ordinary bash, write, or edit tool calls before execution. "
    "Pi tool_execution_start events arrive after execution has begun, so Tether only shows them as activity. "
    "Pi extension UI confirmations are still interactive."
)

_CAPABILITIES: dict[str, AdapterPermissionCapabilities] = {
    "pi_rpc": AdapterPermissionCapabilities(
        adapter="pi_rpc",
        approval_modes_supported=False,
        initial_approval_policy_supported=False,
        approval_mode_updates="unsupported",
        ordinary_tool_approvals=False,
        ordinary_tool_approval_timing="post_start_notification",
        extension_ui_permissions=True,
        unsupported_approval_mode_message=_PI_UNSUPPORTED_MESSAGE,
    ),
    "claude_subprocess": AdapterPermissionCapabilities(
        adapter="claude_subprocess",
        approval_modes_supported=True,
        initial_approval_policy_supported=True,
        approval_mode_updates="live",
        ordinary_tool_approvals=True,
        ordinary_tool_approval_timing="pre_execution",
        extension_ui_permissions=False,
        approval_mode_runner_update=True,
    ),
    "claude_api": AdapterPermissionCapabilities(
        adapter="claude_subprocess",
        approval_modes_supported=True,
        initial_approval_policy_supported=True,
        approval_mode_updates="live",
        ordinary_tool_approvals=True,
        ordinary_tool_approval_timing="pre_execution",
        extension_ui_permissions=False,
        approval_mode_runner_update=True,
    ),
    "claude_auto": AdapterPermissionCapabilities(
        adapter="claude_auto",
        approval_modes_supported=True,
        initial_approval_policy_supported=True,
        approval_mode_updates="live",
        ordinary_tool_approvals=True,
        ordinary_tool_approval_timing="pre_execution",
        extension_ui_permissions=False,
        approval_mode_runner_update=True,
    ),
    "codex_sdk_sidecar": AdapterPermissionCapabilities(
        adapter="codex_sdk_sidecar",
        approval_modes_supported=False,
        initial_approval_policy_supported=True,
        approval_mode_updates="unsupported",
        ordinary_tool_approvals=False,
        ordinary_tool_approval_timing="not_applicable",
        extension_ui_permissions=False,
        unsupported_approval_mode_message="Codex can receive an initial backend approval policy, but the sidecar does not route tool approval requests back to Tether and active session policy changes are not supported.",
    ),
    "opencode": AdapterPermissionCapabilities(
        adapter="opencode",
        approval_modes_supported=False,
        initial_approval_policy_supported=False,
        approval_mode_updates="unsupported",
        ordinary_tool_approvals=False,
        ordinary_tool_approval_timing="not_applicable",
        extension_ui_permissions=False,
        unsupported_approval_mode_message="OpenCode currently auto-approves permissions in its sidecar, so Tether cannot enforce interactive approvals for it.",
    ),
    "automation": AdapterPermissionCapabilities(
        adapter="automation",
        approval_modes_supported=False,
        initial_approval_policy_supported=False,
        approval_mode_updates="unsupported",
        ordinary_tool_approvals=False,
        ordinary_tool_approval_timing="not_applicable",
        extension_ui_permissions=False,
        unsupported_approval_mode_message="The automation adapter runs predefined local scripts and has no interactive tool approval mode.",
    ),
    "litellm": AdapterPermissionCapabilities(
        adapter="litellm",
        approval_modes_supported=False,
        initial_approval_policy_supported=False,
        approval_mode_updates="unsupported",
        ordinary_tool_approvals=False,
        ordinary_tool_approval_timing="not_applicable",
        extension_ui_permissions=False,
        unsupported_approval_mode_message="The LiteLLM adapter executes its configured tools directly and has no interactive approval gate.",
    ),
}


def permission_capabilities_for_adapter(
    adapter: str | None,
) -> AdapterPermissionCapabilities:
    """Return permission capabilities for a public or canonical adapter name."""
    normalized = normalize_adapter_name(adapter) or "pi_rpc"
    capabilities = _CAPABILITIES.get(normalized)
    if capabilities:
        return capabilities

    return AdapterPermissionCapabilities(
        adapter=normalized,
        approval_modes_supported=False,
        initial_approval_policy_supported=False,
        approval_mode_updates="unsupported",
        ordinary_tool_approvals=False,
        ordinary_tool_approval_timing="not_applicable",
        extension_ui_permissions=False,
        unsupported_approval_mode_message=f"Adapter '{normalized}' does not advertise permission-mode support.",
    )


def all_permission_capabilities() -> dict[str, dict[str, object]]:
    """Return public capabilities for all known adapters."""
    return {
        name: capabilities.to_public_dict()
        for name, capabilities in sorted(_CAPABILITIES.items())
        if name != "claude_api"
    }
