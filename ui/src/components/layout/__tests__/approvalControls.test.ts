// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import AppHeader from '../AppHeader.vue';
import Settings from '@/views/Settings.vue';
import { getConfigCapabilities, updateSessionApprovalMode } from '@/api';
import type { ConfigCapabilities, PermissionCapabilities, Session } from '@/api';

vi.mock('@/api', async (importOriginal) => {
  const api = await importOriginal<typeof import('@/api')>();
  return {
    ...api,
    getConfigCapabilities: vi.fn(),
    updateSessionApprovalMode: vi.fn(),
  };
});

function capabilities(overrides: Partial<PermissionCapabilities>): PermissionCapabilities {
  return {
    adapter: 'unknown',
    approval_modes_supported: false,
    initial_approval_policy_supported: false,
    approval_mode_updates: 'unsupported',
    ordinary_tool_approvals: false,
    ordinary_tool_approval_timing: 'not_applicable',
    extension_ui_permissions: false,
    unsupported_approval_mode_message: null,
    ...overrides,
  };
}

const pi = capabilities({ adapter: 'pi_rpc', extension_ui_permissions: true });
const codex = capabilities({ adapter: 'codex_sdk_sidecar', initial_approval_policy_supported: true });
const opencode = capabilities({ adapter: 'opencode' });
const claude = capabilities({
  adapter: 'claude_subprocess',
  approval_modes_supported: true,
  initial_approval_policy_supported: true,
  approval_mode_updates: 'live',
  ordinary_tool_approvals: true,
  ordinary_tool_approval_timing: 'pre_execution',
});

function session(permissionCapabilities: PermissionCapabilities | undefined): Session {
  return {
    id: 'session-1',
    state: 'AWAITING_INPUT',
    name: 'Test session',
    created_at: '2026-01-01T00:00:00Z',
    started_at: null,
    ended_at: null,
    last_activity_at: '2026-01-01T00:00:00Z',
    exit_code: null,
    summary: null,
    runner_header: null,
    runner_type: null,
    runner_session_id: null,
    directory: null,
    directory_has_git: false,
    message_count: 0,
    has_pending_permission: false,
    approval_mode: null,
    bridge_verbosity: null,
    effective_bridge_verbosity: 'minimal',
    bridge_buffer_max_seconds: null,
    effective_bridge_buffer_max_seconds: null,
    permission_capabilities: permissionCapabilities,
  };
}

let wrapper: VueWrapper | undefined;
afterEach(() => {
  wrapper?.unmount();
  wrapper = undefined;
  localStorage.clear();
  vi.clearAllMocks();
});

describe('rendered approval controls', () => {
  const unsupported: [string, PermissionCapabilities | undefined][] = [
    ['Pi', pi],
    ['Codex', codex],
    ['OpenCode', opencode],
    ['older server', undefined],
  ];

  it.each(unsupported)('disables session policy changes for %s', async (_name, capability) => {
    wrapper = mount(AppHeader, {
      props: {
        activeSession: session(capability),
        hasActiveSession: true,
        syncing: false,
        statusDot: 'bg-green-500',
      },
    });
    await wrapper.get('[aria-label="Tool approval mode"]').trigger('click');
    const choices = wrapper.findAll('button:disabled');
    expect(choices).toHaveLength(3);
    for (const choice of choices) {
      await choice.trigger('click');
    }
    expect(updateSessionApprovalMode).not.toHaveBeenCalled();
    expect(wrapper.emitted('update:session')).toBeUndefined();
  });

  it('routes a supported Claude policy change through the API', async () => {
    const current = session(claude);
    vi.mocked(updateSessionApprovalMode).mockResolvedValue({ ...current, approval_mode: 0 });
    wrapper = mount(AppHeader, {
      props: {
        activeSession: current,
        hasActiveSession: true,
        syncing: false,
        statusDot: 'bg-green-500',
      },
    });
    await wrapper.get('[aria-label="Tool approval mode"]').trigger('click');
    expect(wrapper.findAll('button:disabled')).toHaveLength(0);
    await wrapper.get('div.w-56 button').trigger('click');
    await flushPromises();
    expect(updateSessionApprovalMode).toHaveBeenCalledWith('session-1', 0);
    expect(wrapper.emitted('update:session')).toHaveLength(1);
  });

  const defaults: [string, PermissionCapabilities | undefined, boolean][] = [
    ['Pi', pi, false],
    ['Codex initial backend policy', codex, true],
    ['OpenCode', opencode, false],
    ['Claude', claude, true],
    ['older server', undefined, false],
  ];

  it.each(defaults)('shows the correct global policy controls for %s', async (_name, capability, enabled) => {
    const config: ConfigCapabilities = { default_adapter: null, permission_capabilities: {} };
    if (capability) {
      config.default_adapter = capability.adapter;
      config.permission_capabilities[capability.adapter] = capability;
    }
    vi.mocked(getConfigCapabilities).mockResolvedValue(config);
    wrapper = mount(Settings, { global: { stubs: { Dashboard: true } } });
    await flushPromises();
    const choices = wrapper.findAll<HTMLInputElement>('input[name="approvalMode"]');
    expect(choices).toHaveLength(3);
    for (const choice of choices) {
      expect(choice.element.disabled).toBe(!enabled);
    }
    if (capability === codex) {
      expect(wrapper.text()).toContain('Tether cannot approve individual tools');
    }
  });
});
