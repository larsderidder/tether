import type { SessionState } from '@/api';

const sessionStates: ReadonlySet<string> = new Set<SessionState>([
  'CREATED',
  'RUNNING',
  'AWAITING_INPUT',
  'INTERRUPTING',
  'ERROR',
]);

/** Reject unknown states received from an event stream. */
export function isSessionState(value: unknown): value is SessionState {
  return typeof value === 'string' && sessionStates.has(value);
}
