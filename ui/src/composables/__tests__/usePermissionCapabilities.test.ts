import { describe, expect, it } from 'vitest';
import type { PermissionCapabilities, Session } from '@/api';
import {
  canChangeToolApprovalMode,
  permissionCapabilityDescription,
  permissionCapabilityLabel,
} from '../usePermissionCapabilities';

const baseCapabilities: PermissionCapabilities = {
  adapter: 'unknown',
  approval_modes_supported: false,
  initial_approval_policy_supported: false,
  approval_mode_updates: 'unsupported',
  ordinary_tool_approvals: false,
  ordinary_tool_approval_timing: 'not_applicable',
  extension_ui_permissions: false,
  unsupported_approval_mode_message: null,
};

function sessionWith(capabilities?: PermissionCapabilities): Session {
  return {
    id: 'sess_test',
    state: 'CREATED',
    name: null,
    created_at: '2026-01-01T00:00:00Z',
    started_at: null,
    ended_at: null,
    last_activity_at: '2026-01-01T00:00:00Z',
    exit_code: null,
    summary: null,
    runner_header: null,
    runner_type: null,
    runner_session_id: null,
    adapter: capabilities?.adapter ?? null,
    model: null,
    directory: null,
    directory_has_git: false,
    message_count: 0,
    has_pending_permission: false,
    approval_mode: null,
    bridge_verbosity: null,
    effective_bridge_verbosity: 'medium',
    bridge_buffer_max_seconds: null,
    effective_bridge_buffer_max_seconds: null,
    permission_capabilities: capabilities as PermissionCapabilities,
  };
}

describe('permission capability controls', () => {
  it('fails closed when an older server omits capabilities', () => {
    const session = sessionWith(undefined);

    expect(canChangeToolApprovalMode(session)).toBe(false);
  });

  it.each([
    [
      'pi_rpc',
      {
        ...baseCapabilities,
        adapter: 'pi_rpc',
        ordinary_tool_approval_timing: 'post_start_notification' as const,
        extension_ui_permissions: true,
        unsupported_approval_mode_message: 'Pi cannot pause ordinary tools.',
      },
      'Extension confirmations only',
    ],
    [
      'codex_sdk_sidecar',
      {
        ...baseCapabilities,
        adapter: 'codex_sdk_sidecar',
        initial_approval_policy_supported: true,
        unsupported_approval_mode_message: 'Initial policy only.',
      },
      'Initial backend policy only',
    ],
    [
      'opencode',
      {
        ...baseCapabilities,
        adapter: 'opencode',
        unsupported_approval_mode_message: 'OpenCode auto-approves.',
      },
      'Approval controls unavailable',
    ],
  ])('disables ordinary tool approval controls for %s', (_adapter, capabilities, label) => {
    expect(canChangeToolApprovalMode(sessionWith(capabilities))).toBe(false);
    expect(permissionCapabilityLabel(capabilities)).toBe(label);
    expect(permissionCapabilityDescription(capabilities)).toContain(
      capabilities.unsupported_approval_mode_message || ''
    );
  });

  it('preserves approval controls for Claude subprocess sessions', () => {
    const capabilities: PermissionCapabilities = {
      ...baseCapabilities,
      adapter: 'claude_subprocess',
      approval_modes_supported: true,
      initial_approval_policy_supported: true,
      approval_mode_updates: 'live',
      ordinary_tool_approvals: true,
      ordinary_tool_approval_timing: 'pre_execution',
    };

    expect(canChangeToolApprovalMode(sessionWith(capabilities))).toBe(true);
    expect(permissionCapabilityLabel(capabilities)).toBe('Tool approvals');
  });
});
