/**
 * Tests for codex.ts runTurn timeout functionality.
 *
 * This file tests the timeout behavior of runTurn with a non-zero timeout setting.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { SessionState } from "./types.js";

// Control variable for timeout setting
let timeoutSeconds = 1;

// Create mock functions that we can control
const mockRunStreamed = vi.fn();
const mockStartThread = vi.fn(() => ({
  runStreamed: mockRunStreamed,
}));

// Mock the session emit functions
const mockEmitError = vi.fn();
const mockEmitHeartbeat = vi.fn();
const mockEmitExit = vi.fn();
const mockEmitHeader = vi.fn();

vi.mock("./session.js", () => ({
  emit: vi.fn(),
  emitOutput: vi.fn(),
  emitMetadata: vi.fn(),
  emitError: mockEmitError,
  emitHeartbeat: mockEmitHeartbeat,
  emitExit: mockEmitExit,
  emitHeader: mockEmitHeader,
}));

vi.mock("./workdir.js", () => ({
  ensureWorkdir: vi.fn().mockResolvedValue("/tmp/test-workdir"),
}));

// Mock settings with dynamic timeout
vi.mock("./settings.js", () => ({
  settings: {
    codexModel: () => null,
    codexSandboxMode: () => null,
    networkAccessEnabled: () => true,
    codexApprovalPolicy: () => null,
    turnTimeoutSeconds: () => timeoutSeconds,
    codexBin: () => null,
    logLevel: () => "silent",
    logPretty: () => false,
    host: () => "127.0.0.1",
    port: () => 8788,
    token: () => "",
  },
}));

vi.mock("../../codex-src/sdk/typescript/src/index.js", () => ({
  Codex: vi.fn(() => ({
    startThread: mockStartThread,
    resumeThread: vi.fn(),
  })),
}));

function createTestSession(): SessionState {
  return {
    id: "test-session",
    running: false,
    pendingInputs: [],
    subscribers: new Set(),
    eventBuffer: [],
  };
}

// Helper to create an event stream - runStreamed returns { events: AsyncIterable }
function createMockEventStream(events: unknown[] = []) {
  let index = 0;
  return {
    events: {
      [Symbol.asyncIterator]() {
        return {
          next: async () => {
            if (index < events.length) {
              return { done: false, value: events[index++] };
            }
            return { done: true, value: undefined };
          },
        };
      },
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  timeoutSeconds = 1;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("runTurn timeout", () => {
  it("sets timeout timer when turnTimeoutSeconds > 0", async () => {
    const { runTurn } = await import("./codex.js");
    const session = createTestSession();

    // Create a stream that waits for abort signal
    mockRunStreamed.mockImplementation((_input: string, options: { signal?: AbortSignal }) => {
      return {
        events: {
          [Symbol.asyncIterator]() {
            return {
              next: async () => {
                // Wait for abort or timeout
                if (options.signal?.aborted) {
                  return { done: true, value: undefined };
                }
                // Wait for abort
                await new Promise<void>((resolve) => {
                  if (options.signal?.aborted) {
                    resolve();
                    return;
                  }
                  options.signal?.addEventListener("abort", () => resolve(), { once: true });
                  // Also set a safety timeout
                  setTimeout(resolve, 2000);
                });
                return { done: true, value: undefined };
              },
            };
          },
        },
      };
    });

    await runTurn(session, "test input", 0);

    expect(mockEmitError).toHaveBeenCalledWith(
      session,
      "TIMEOUT",
      "Runner turn timed out",
      expect.anything(),
    );
  });

  it("sets abortReason to timeout when timeout fires", async () => {
    vi.resetModules();
    const { runTurn } = await import("./codex.js");
    const session = createTestSession();

    mockRunStreamed.mockImplementation((_input: string, options: { signal?: AbortSignal }) => {
      return {
        events: {
          [Symbol.asyncIterator]() {
            return {
              next: async () => {
                if (options.signal?.aborted) {
                  return { done: true, value: undefined };
                }
                await new Promise<void>((resolve) => {
                  if (options.signal?.aborted) {
                    resolve();
                    return;
                  }
                  options.signal?.addEventListener("abort", () => resolve(), { once: true });
                  setTimeout(resolve, 2000);
                });
                return { done: true, value: undefined };
              },
            };
          },
        },
      };
    });

    await runTurn(session, "test input", 0);

    // abortReason is cleared in finally block, but we can verify timeout behavior
    // by checking that the TIMEOUT error was emitted
    expect(mockEmitError).toHaveBeenCalledWith(
      session,
      "TIMEOUT",
      expect.any(String),
      expect.anything(),
    );
  });

  it("does not set timeout timer when turnTimeoutSeconds is 0", async () => {
    timeoutSeconds = 0;
    vi.resetModules();
    const { runTurn } = await import("./codex.js");
    const session = createTestSession();

    mockRunStreamed.mockReturnValue(createMockEventStream());

    await runTurn(session, "test input", 0);

    // No timeout error should be emitted
    expect(mockEmitError).not.toHaveBeenCalled();
  });
});
