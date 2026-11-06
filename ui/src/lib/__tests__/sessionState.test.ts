import { describe, expect, it } from 'vitest';
import { isSessionState } from '../sessionState';

describe('isSessionState', () => {
  it.each(['CREATED', 'RUNNING', 'AWAITING_INPUT', 'INTERRUPTING', 'ERROR'])(
    'accepts %s',
    (state) => {
      expect(isSessionState(state)).toBe(true);
    },
  );

  it.each(['running', 'UNKNOWN', '', null, undefined, 1, {}, ['RUNNING']])(
    'rejects invalid state %j',
    (state) => {
      expect(isSessionState(state)).toBe(false);
    },
  );
});
