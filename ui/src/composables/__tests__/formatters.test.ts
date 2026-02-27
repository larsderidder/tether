import { describe, it, expect } from 'vitest';
import {
  formatState,
  formatTime,
  formatSessionId,
  getStatusDotClass
} from '../formatters';

describe('formatState', () => {
  it('returns empty string for undefined', () => {
    expect(formatState(undefined)).toBe('');
  });

  it('maps known states to labels', () => {
    expect(formatState('CREATED')).toBe('Ready');
    expect(formatState('RUNNING')).toBe('Running');
    expect(formatState('AWAITING_INPUT')).toBe('Awaiting input');
    expect(formatState('INTERRUPTING')).toBe('Interrupting');
    expect(formatState('ERROR')).toBe('Error');
  });

  it('formats unknown states by lowercasing and replacing underscores', () => {
    expect(formatState('SOME_UNKNOWN_STATE')).toBe('some unknown state');
  });
});

describe('formatTime', () => {
  it('returns a relative time string', () => {
    const now = new Date().toISOString();
    const result = formatTime(now);
    expect(result).toContain('ago');
  });
});

describe('formatSessionId', () => {
  it('returns first 8 characters of the ID', () => {
    expect(formatSessionId('abc123def456')).toBe('abc123de');
    expect(formatSessionId('12345678901234567890')).toBe('12345678');
  });

  it('returns full string if less than 8 characters', () => {
    expect(formatSessionId('abc')).toBe('abc');
  });
});

describe('getStatusDotClass', () => {
  it('returns correct class for RUNNING', () => {
    expect(getStatusDotClass('RUNNING')).toBe('bg-emerald-500');
  });

  it('returns correct class for AWAITING_INPUT with pulse', () => {
    expect(getStatusDotClass('AWAITING_INPUT')).toBe('bg-amber-400 animate-pulse');
  });

  it('returns correct class for INTERRUPTING', () => {
    expect(getStatusDotClass('INTERRUPTING')).toBe('bg-amber-500');
  });

  it('returns correct class for ERROR', () => {
    expect(getStatusDotClass('ERROR')).toBe('bg-rose-500');
  });

  it('returns correct class for CREATED', () => {
    expect(getStatusDotClass('CREATED')).toBe('bg-blue-400');
  });

  it('returns empty string for undefined', () => {
    expect(getStatusDotClass(undefined)).toBe('');
  });

  it('returns empty string for unknown state', () => {
    expect(getStatusDotClass('UNKNOWN')).toBe('');
  });
});
