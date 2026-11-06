// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mount } from '@vue/test-utils';
import { defineComponent, h } from 'vue';
import * as api from '@/api';
import { useAuth } from '../useAuth';

vi.mock('@/api', () => ({
  AUTH_REQUIRED_EVENT: 'tether:auth-required',
  getToken: vi.fn(() => 'stored-token'),
  setToken: vi.fn(),
}));

function mountAuth() {
  return mount(defineComponent({
    setup() {
      return useAuth();
    },
    render() {
      return h('div');
    },
  }));
}

let wrapper: ReturnType<typeof mountAuth>;
beforeEach(() => {
  vi.mocked(api.getToken).mockReturnValue('stored-token');
  wrapper = mountAuth();
});

afterEach(() => {
  vi.unstubAllGlobals();
  wrapper.vm.closeModal();
  wrapper.vm.authRequired = false;
  wrapper.vm.tokenSaved = false;
  wrapper.unmount();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe('useAuth', () => {
  it('opens the modal and refreshes the token after an auth-required event', () => {
    vi.mocked(api.getToken).mockReturnValue('updated-token');
    expect(wrapper.vm.authRequired).toBe(false);
    expect(wrapper.vm.modalOpen).toBe(false);
    window.dispatchEvent(new Event(api.AUTH_REQUIRED_EVENT));
    expect(wrapper.vm.authRequired).toBe(true);
    expect(wrapper.vm.modalOpen).toBe(true);
    expect(wrapper.vm.tokenInput).toBe('updated-token');
  });

  it('refreshes the stored token when opening the modal manually', () => {
    vi.mocked(api.getToken).mockReturnValue('replacement-token');
    wrapper.vm.openModal();
    expect(wrapper.vm.tokenInput).toBe('replacement-token');
    expect(wrapper.vm.modalOpen).toBe(true);
  });

  it('saves a trimmed token and clears auth state before reloading', () => {
    window.dispatchEvent(new Event(api.AUTH_REQUIRED_EVENT));
    wrapper.vm.tokenInput = '  replacement-token  ';
    const reload = vi.fn(() => {
      expect(wrapper.vm.authRequired).toBe(false);
      expect(wrapper.vm.modalOpen).toBe(false);
      expect(wrapper.vm.tokenSaved).toBe(true);
    });
    vi.stubGlobal('window', { location: { reload } });
    wrapper.vm.saveToken();
    expect(api.setToken).toHaveBeenCalledWith('replacement-token');
    expect(reload).toHaveBeenCalledOnce();
  });

  it('closes the modal without storing a token', () => {
    wrapper.vm.openModal();
    wrapper.vm.closeModal();
    expect(wrapper.vm.modalOpen).toBe(false);
    expect(api.setToken).not.toHaveBeenCalled();
  });

  it('removes the auth-required listener when the component unmounts', () => {
    const remove = vi.spyOn(window, 'removeEventListener');
    wrapper.unmount();
    expect(remove).toHaveBeenCalledWith(api.AUTH_REQUIRED_EVENT, expect.any(Function));
    window.dispatchEvent(new Event(api.AUTH_REQUIRED_EVENT));
    expect(wrapper.vm.authRequired).toBe(false);
    expect(wrapper.vm.modalOpen).toBe(false);
  });
});
