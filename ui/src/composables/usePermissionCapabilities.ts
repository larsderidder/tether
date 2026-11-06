import type { PermissionCapabilities, Session } from "@/api";

export const FAIL_CLOSED_PERMISSION_CAPABILITIES: PermissionCapabilities = {
  adapter: "unknown",
  approval_modes_supported: false,
  initial_approval_policy_supported: false,
  approval_mode_updates: "unsupported",
  ordinary_tool_approvals: false,
  ordinary_tool_approval_timing: "not_applicable",
  extension_ui_permissions: false,
  unsupported_approval_mode_message: "This server did not advertise permission capabilities, so Tether disables approval controls.",
};

export function permissionCapabilitiesForSession(session: Session | null | undefined): PermissionCapabilities {
  const capabilities = session?.permission_capabilities;
  if (!capabilities || capabilities.approval_mode_updates === undefined) {
    return FAIL_CLOSED_PERMISSION_CAPABILITIES;
  }
  return capabilities;
}

export function canChangeToolApprovalMode(session: Session | null | undefined): boolean {
  const capabilities = permissionCapabilitiesForSession(session);
  return Boolean(
    capabilities.approval_modes_supported &&
    capabilities.ordinary_tool_approvals &&
    capabilities.approval_mode_updates !== "unsupported"
  );
}

export function canUseInitialApprovalPolicy(capabilities: PermissionCapabilities): boolean {
  return Boolean(capabilities.initial_approval_policy_supported);
}

export function permissionCapabilityLabel(capabilities: PermissionCapabilities): string {
  if (capabilities.ordinary_tool_approvals) {
    return "Tool approvals";
  }
  if (capabilities.extension_ui_permissions) {
    return "Extension confirmations only";
  }
  if (capabilities.initial_approval_policy_supported) {
    return "Initial backend policy only";
  }
  return "Approval controls unavailable";
}

export function permissionCapabilityDescription(capabilities: PermissionCapabilities): string {
  if (capabilities.ordinary_tool_approvals) {
    if (capabilities.approval_mode_updates === "live") {
      return "Tether can receive and answer tool approval requests for this adapter. Active sessions accept policy changes.";
    }
    return "Tether can receive and answer tool approval requests for this adapter. Policy changes apply to future turns.";
  }
  if (capabilities.extension_ui_permissions) {
    return capabilities.unsupported_approval_mode_message || "Ordinary tools are not gated, but extension UI confirmations still ask for a decision.";
  }
  if (capabilities.initial_approval_policy_supported) {
    return capabilities.unsupported_approval_mode_message || "The adapter accepts an initial backend approval policy, but Tether cannot approve individual tools or change policy for an active session.";
  }
  return capabilities.unsupported_approval_mode_message || FAIL_CLOSED_PERMISSION_CAPABILITIES.unsupported_approval_mode_message || "Approval controls are unavailable.";
}

export function approvalModeControlTitle(session: Session | null | undefined): string {
  const capabilities = permissionCapabilitiesForSession(session);
  if (canChangeToolApprovalMode(session)) {
    return "Change tool approval mode";
  }
  return permissionCapabilityDescription(capabilities);
}
