/**
 * Tests for opencode.ts runTurn timeout behaviour.
 *
 * Separate file so vi.mock can use a mutable timeoutSeconds variable.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { SessionState } from "@tether/sidecar-common/types";
import type { OpencodeServerHandle } from "./session.js";

// Mutable — changed per test
let timeoutSeconds = 1;

const mockEmitError = vi.fn();
const mockEmitHeartbeat = vi.fn();
const mockEmitExit = vi.fn();
const mockEmitMetadata = vi.fn();
const mockEmitHeader = vi.fn();

vi.mock("./session.js", () => ({
  emitOutput: vi.fn(),
  emitMetadata: mockEmitMetadata,
  emitError: mockEmitError,
  emitHeartbeat: mockEmitHeartbeat,
  emitExit: mockEmitExit,
  emitHeader: mockEmitHeader,
  createSessionRegistry: vi.fn(),
}));

vi.mock("./logger.js", () => ({
  logger: { info: vi.fn(), warn: vi.fn(), debug: vi.fn(), error: vi.fn() },
}));

vi.mock("./settings.js", () => ({
  settings: {
    opencodeBin: () => undefined,
    turnTimeoutSeconds: () => timeoutSeconds,
    logLevel: () => "silent",
    logPretty: () => false,
  },
}));

vi.mock("@tether/sidecar-common/workdir", () => ({
  ensureWorkdir: vi.fn().mockResolvedValue("/tmp/test-workdir"),
}));

const mockSessionCreate = vi.fn().mockResolvedValue({ data: { id: "oc-sess-001" } });
const mockPromptAsync = vi.fn().mockResolvedValue({});

/**
 * Build an event stream that blocks until the abort signal fires.
 * This is what keeps runTurn alive so the timeout timer can fire.
 */
function makeBlockingStream(signal: AbortSignal) {
  return {
    stream: {
      [Symbol.asyncIterator]() {
        return {
          next: () =>
            new Promise<{ done: boolean; value: unknown }>((resolve) => {
              if (signal.aborted) {
                resolve({ done: true, value: undefined });
                return;
              }
              signal.addEventListener("abort", () => resolve({ done: true, value: undefined }), {
                once: true,
              });
            }),
        };
      },
    },
  };
}

const mockGlobalEvent = vi.fn();

vi.mock("@opencode-ai/sdk", () => ({
  createOpencodeServer: vi.fn().mockResolvedValue({
    url: "http://127.0.0.1:59998",
    close: vi.fn(),
  }),
  createOpencodeClient: vi.fn(() => ({
    session: { create: mockSessionCreate, promptAsync: mockPromptAsync },
    global: { event: mockGlobalEvent },
  })),
}));

function createTestSession(): SessionState<OpencodeServerHandle> {
  return {
    id: "test-session",
    running: false,
    pendingInputs: [],
    subscribers: new Set(),
    eventBuffer: [],
  };
}

beforeEach(async () => {
  vi.clearAllMocks();
  timeoutSeconds = 1;
  mockPromptAsync.mockResolvedValue({});
  mockSessionCreate.mockResolvedValue({ data: { id: "oc-sess-001" } });

  // Restore createOpencodeServer return value after clearAllMocks resets it.
  const { createOpencodeServer } = await import("@opencode-ai/sdk");
  (createOpencodeServer as ReturnType<typeof vi.fn>).mockResolvedValue({
    url: "http://127.0.0.1:59998",
    close: vi.fn(),
  });

  // Default: blocking stream so the timeout timer fires before the stream ends
  mockGlobalEvent.mockImplementation(async () => {
    return new Promise(() => {}); // never resolves — timeout fires first
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("runTurn timeout", () => {
  it("emits TIMEOUT error when timeout elapses", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    // Make globalEvent return a blocking stream that respects the abort signal
    mockGlobalEvent.mockImplementation(async () => {
      // Wait until runTurn has set session.abortController, then use its signal
      await new Promise((r) => setTimeout(r, 50));
      const signal = session.abortController?.signal;
      return makeBlockingStream(signal ?? new AbortController().signal);
    });

    await runTurn(session, "hello", 2);

    expect(mockEmitError).toHaveBeenCalledWith(
      session,
      "TIMEOUT",
      "Turn timed out",
      expect.anything(),
    );
  }, 15_000);

  it("runs finally cleanup after timeout", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    mockGlobalEvent.mockImplementation(async () => {
      await new Promise((r) => setTimeout(r, 50));
      const signal = session.abortController?.signal;
      return makeBlockingStream(signal ?? new AbortController().signal);
    });

    await runTurn(session, "hello", 2);

    expect(session.running).toBe(false);
    expect(session.heartbeatTimer).toBeUndefined();
    expect(session.timeoutTimer).toBeUndefined();
    expect(mockEmitHeartbeat).toHaveBeenCalledWith(session, true);
  }, 15_000);

  it("does not emit TIMEOUT when turnTimeoutSeconds is 0", async () => {
    timeoutSeconds = 0;

    // Immediately-resolving stream for this test
    mockGlobalEvent.mockResolvedValue({
      stream: {
        [Symbol.asyncIterator]() {
          return {
            next: async () => ({ done: true as const, value: undefined }),
          };
        },
      },
    });

    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();
    await runTurn(session, "hello", 2);

    expect(mockEmitError).not.toHaveBeenCalled();
  });
});
