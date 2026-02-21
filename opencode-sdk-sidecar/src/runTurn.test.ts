/**
 * Tests for opencode.ts runTurn function.
 *
 * Tests ensureServer (via runTurn), the finally-block cleanup, abort handling,
 * pending input draining, and timeout behaviour.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { SessionState } from "@tether/sidecar-common/types";
import type { OpencodeServerHandle } from "./session.js";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockEmitOutput = vi.fn();
const mockEmitMetadata = vi.fn();
const mockEmitError = vi.fn();
const mockEmitHeartbeat = vi.fn();
const mockEmitExit = vi.fn();
const mockEmitHeader = vi.fn();

vi.mock("./session.js", () => ({
  emitOutput: mockEmitOutput,
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
    turnTimeoutSeconds: () => 0,
    logLevel: () => "silent",
    logPretty: () => false,
  },
}));

vi.mock("@tether/sidecar-common/workdir", () => ({
  ensureWorkdir: vi.fn().mockResolvedValue("/tmp/test-workdir"),
}));

// Mock promptAsync and the event stream
const mockPromptAsync = vi.fn().mockResolvedValue({});
const mockGlobalEvent = vi.fn();
const mockSessionCreate = vi.fn().mockResolvedValue({ data: { id: "oc-sess-001" } });

const mockClient = {
  session: {
    create: mockSessionCreate,
    promptAsync: mockPromptAsync,
    postSessionIdPermissionsPermissionId: vi.fn().mockResolvedValue({}),
  },
  global: { event: mockGlobalEvent },
};

const mockServerClose = vi.fn();
const mockServer = { url: "http://127.0.0.1:59999", close: mockServerClose };

vi.mock("@opencode-ai/sdk", () => ({
  createOpencodeServer: vi.fn().mockResolvedValue(mockServer),
  createOpencodeClient: vi.fn().mockReturnValue(mockClient),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function createTestSession(): SessionState<OpencodeServerHandle> {
  return {
    id: "test-session",
    running: false,
    pendingInputs: [],
    subscribers: new Set(),
    eventBuffer: [],
  };
}

/**
 * Build a mock SSE stream that yields the given events then signals idle.
 * The stream auto-terminates so runTurn doesn't block forever.
 */
function makeStream(events: Array<{ type: string; properties: Record<string, unknown> }> = []) {
  const all = [
    ...events,
    // idle event terminates streamEvents loop
    { type: "session.idle", properties: { sessionID: "oc-sess-001" } },
  ];
  let i = 0;
  return {
    stream: {
      [Symbol.asyncIterator]() {
        return {
          next: async () =>
            i < all.length
              ? { done: false as const, value: { payload: all[i++] } }
              : { done: true as const, value: undefined },
        };
      },
    },
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  mockGlobalEvent.mockResolvedValue(makeStream());
});

afterEach(() => {
  vi.useRealTimers();
});

describe("runTurn — finally block cleanup", () => {
  it("sets running=false after turn completes", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    await runTurn(session, "hello", 2);
    await vi.runAllTimersAsync();

    expect(session.running).toBe(false);
  });

  it("clears heartbeatTimer in finally", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    await runTurn(session, "hello", 2);
    await vi.runAllTimersAsync();

    expect(session.heartbeatTimer).toBeUndefined();
  });

  it("clears timeoutTimer in finally", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    await runTurn(session, "hello", 2);
    await vi.runAllTimersAsync();

    expect(session.timeoutTimer).toBeUndefined();
  });

  it("clears abortController in finally", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    await runTurn(session, "hello", 2);
    await vi.runAllTimersAsync();

    expect(session.abortController).toBeUndefined();
  });

  it("clears abortReason in finally", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    await runTurn(session, "hello", 2);
    await vi.runAllTimersAsync();

    expect(session.abortReason).toBeUndefined();
  });

  it("emits heartbeat with done=true in finally", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    await runTurn(session, "hello", 2);
    await vi.runAllTimersAsync();

    expect(mockEmitHeartbeat).toHaveBeenCalledWith(session, true);
  });

  it("emits duration_ms metadata in finally", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    await runTurn(session, "hello", 2);
    await vi.runAllTimersAsync();

    expect(mockEmitMetadata).toHaveBeenCalledWith(
      session,
      "duration_ms",
      expect.any(Number),
      expect.any(String),
    );
  });

  it("emits exit(0) when no pending inputs remain", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    await runTurn(session, "hello", 2);
    await vi.runAllTimersAsync();

    expect(mockEmitExit).toHaveBeenCalledWith(session, 0);
  });
});

describe("runTurn — pending input draining", () => {
  it("processes queued inputs recursively after turn completes", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();
    session.pendingInputs = ["queued-1", "queued-2"];

    await runTurn(session, "first", 2);
    await vi.runAllTimersAsync();

    expect(session.pendingInputs).toEqual([]);
  });

  it("calls promptAsync for each queued input", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();
    session.pendingInputs = ["queued-1"];

    await runTurn(session, "first", 2);
    await vi.runAllTimersAsync();

    // promptAsync called once for "first", once for "queued-1"
    expect(mockPromptAsync).toHaveBeenCalledTimes(2);
  });

  it("does not emit exit until all pending inputs are drained", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();
    session.pendingInputs = ["queued-1"];

    await runTurn(session, "first", 2);
    await vi.runAllTimersAsync();

    // Exit only emitted once, after last turn
    expect(mockEmitExit).toHaveBeenCalledTimes(1);
    expect(mockEmitExit).toHaveBeenCalledWith(session, 0);
  });
});

describe("runTurn — abort handling", () => {
  it("swallows AbortError silently without calling emitError", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    const abortError = new Error("Aborted");
    abortError.name = "AbortError";
    mockPromptAsync.mockRejectedValueOnce(abortError);

    await runTurn(session, "hello", 2);
    await vi.runAllTimersAsync();

    expect(mockEmitError).not.toHaveBeenCalled();
    expect(session.running).toBe(false);
  });

  it("calls emitError for non-abort errors", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    mockPromptAsync.mockRejectedValueOnce(new Error("Network failure"));

    await runTurn(session, "hello", 2);
    await vi.runAllTimersAsync();

    expect(mockEmitError).toHaveBeenCalledWith(
      session,
      "INTERNAL_ERROR",
      expect.stringContaining("Network failure"),
      expect.anything(),
    );
  });

  it("still runs finally cleanup after abort", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    const abortError = new Error("Aborted");
    abortError.name = "AbortError";
    mockPromptAsync.mockRejectedValueOnce(abortError);

    await runTurn(session, "hello", 2);
    await vi.runAllTimersAsync();

    expect(session.running).toBe(false);
    expect(session.heartbeatTimer).toBeUndefined();
    expect(mockEmitHeartbeat).toHaveBeenCalledWith(session, true);
  });
});

describe("runTurn — ensureServer (via runTurn)", () => {
  it("reuses existing server handle when workdir matches", async () => {
    const { createOpencodeServer } = await import("@opencode-ai/sdk");
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();
    session.workdir = "/my/project";

    // First turn — creates server
    await runTurn(session, "first", 2);
    await vi.runAllTimersAsync();

    const callsAfterFirst = (createOpencodeServer as ReturnType<typeof vi.fn>).mock.calls.length;

    // Second turn — same workdir, should reuse
    await runTurn(session, "second", 2);
    await vi.runAllTimersAsync();

    expect((createOpencodeServer as ReturnType<typeof vi.fn>).mock.calls.length).toBe(
      callsAfterFirst,
    );
  });

  it("closes old server and creates new one when workdir changes", async () => {
    const { createOpencodeServer } = await import("@opencode-ai/sdk");
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();
    session.workdir = "/project-a";

    await runTurn(session, "first", 2);
    await vi.runAllTimersAsync();

    const callsAfterFirst = (createOpencodeServer as ReturnType<typeof vi.fn>).mock.calls.length;

    // Change workdir to simulate directory switch
    session.workdir = "/project-b";

    await runTurn(session, "second", 2);
    await vi.runAllTimersAsync();

    expect((createOpencodeServer as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(
      callsAfterFirst,
    );
    expect(mockServerClose).toHaveBeenCalled();
  });

  it("stores opencode session id on first turn", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    await runTurn(session, "hello", 2);
    await vi.runAllTimersAsync();

    expect(session.threadId).toBe("oc-sess-001");
  });

  it("reuses stored opencode session id on subsequent turns", async () => {
    const { runTurn } = await import("./opencode.js");
    const session = createTestSession();

    await runTurn(session, "first", 2);
    await vi.runAllTimersAsync();

    const createCallsAfterFirst = mockSessionCreate.mock.calls.length;

    await runTurn(session, "second", 2);
    await vi.runAllTimersAsync();

    // session.create should not be called again
    expect(mockSessionCreate.mock.calls.length).toBe(createCallsAfterFirst);
  });
});


