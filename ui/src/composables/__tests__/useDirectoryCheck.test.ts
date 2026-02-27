import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useDirectoryCheck } from '../useDirectoryCheck';
import * as api from '@/api';

// Mock the API module
vi.mock('@/api', () => ({
  checkDirectory: vi.fn(),
}));

describe('useDirectoryCheck', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('initializes with empty state', () => {
    const { input, checking, probe, error } = useDirectoryCheck();

    expect(input.value).toBe('');
    expect(checking.value).toBe(false);
    expect(probe.value).toBeNull();
    expect(error.value).toBe('');
  });

  it('clears state when input is empty', async () => {
    const { input, probe, error, checking } = useDirectoryCheck();

    input.value = '/some/path';
    await vi.advanceTimersByTimeAsync(500);

    input.value = '';
    await vi.advanceTimersByTimeAsync(500);

    expect(probe.value).toBeNull();
    expect(error.value).toBe('');
    expect(checking.value).toBe(false);
  });

  it('debounces API calls', async () => {
    const mockCheck = vi.mocked(api.checkDirectory);
    mockCheck.mockResolvedValue({ path: '/test', exists: true, is_git: false });

    const { input } = useDirectoryCheck(400);

    input.value = '/test';
    expect(mockCheck).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(200);
    expect(mockCheck).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(250);
    expect(mockCheck).toHaveBeenCalledWith('/test');
  });

  it('sets checking to true during debounce', async () => {
    const mockCheck = vi.mocked(api.checkDirectory);
    mockCheck.mockResolvedValue({ path: '/test', exists: true, is_git: false });

    const { input, checking } = useDirectoryCheck();

    input.value = '/test';
    // Wait for watcher to fire
    await vi.advanceTimersByTimeAsync(0);
    expect(checking.value).toBe(true);

    await vi.advanceTimersByTimeAsync(500);
    expect(checking.value).toBe(false);
  });

  it('sets probe on successful check', async () => {
    const mockCheck = vi.mocked(api.checkDirectory);
    mockCheck.mockResolvedValue({ path: '/project', exists: true, is_git: true });

    const { input, probe, error } = useDirectoryCheck();

    input.value = '/project';
    await vi.advanceTimersByTimeAsync(500);

    expect(probe.value).toEqual({ path: '/project', exists: true, is_git: true });
    expect(error.value).toBe('');
  });

  it('sets error when directory does not exist', async () => {
    const mockCheck = vi.mocked(api.checkDirectory);
    mockCheck.mockResolvedValue({ path: '/invalid', exists: false, is_git: false });

    const { input, probe, error } = useDirectoryCheck();

    input.value = '/invalid';
    await vi.advanceTimersByTimeAsync(500);

    expect(probe.value?.exists).toBe(false);
    expect(error.value).toBe('Directory not found');
  });

  it('handles API errors', async () => {
    const mockCheck = vi.mocked(api.checkDirectory);
    mockCheck.mockRejectedValue(new Error('Network error'));

    const { input, probe, error } = useDirectoryCheck();

    input.value = '/test';
    await vi.advanceTimersByTimeAsync(500);

    expect(probe.value).toBeNull();
    expect(error.value).toBe('Error: Network error');
  });

  it('cancels pending check on rapid input changes', async () => {
    const mockCheck = vi.mocked(api.checkDirectory);
    mockCheck.mockResolvedValue({ path: '/final', exists: true, is_git: false });

    const { input } = useDirectoryCheck();

    input.value = '/first';
    await vi.advanceTimersByTimeAsync(100);

    input.value = '/second';
    await vi.advanceTimersByTimeAsync(100);

    input.value = '/final';
    await vi.advanceTimersByTimeAsync(500);

    // Only the final path should be checked
    expect(mockCheck).toHaveBeenCalledTimes(1);
    expect(mockCheck).toHaveBeenCalledWith('/final');
  });

  it('check() bypasses debounce for immediate check', async () => {
    const mockCheck = vi.mocked(api.checkDirectory);
    mockCheck.mockResolvedValue({ path: '/immediate', exists: true, is_git: false });

    const { check, probe } = useDirectoryCheck();

    await check('/immediate');

    expect(mockCheck).toHaveBeenCalledWith('/immediate');
    expect(probe.value?.exists).toBe(true);
  });

  it('trims whitespace from input', async () => {
    const mockCheck = vi.mocked(api.checkDirectory);
    mockCheck.mockResolvedValue({ path: '/project', exists: true, is_git: false });

    const { input } = useDirectoryCheck();

    input.value = '  /project  ';
    await vi.advanceTimersByTimeAsync(500);

    expect(mockCheck).toHaveBeenCalledWith('/project');
  });
});
