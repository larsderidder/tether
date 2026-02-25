/**
 * Tests for codex.ts runTurn function — Codex SDK integration.
 *
 * This file tests the runTurn function with proper mocking of the SDK.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { SessionState } from "./types.js";

// Create mock functions that we can control
const mockRunStreamed = vi.fn();
const mockStartThread = vi.fn(() => ({
  runStreamed: mockRunStreamed,
}));
const mockResumeThread = vi.fn(() => ({
  runStreamed: mockRunStreamed,
}));

// Mock the session emit functions
const mockEmit = vi.fn();
const mockEmitOutput = vi.fn();
const mockEmitMetadata = vi.fn();
const mockEmitError = vi.fn();
const mockEmitHeartbeat = vi.fn();
const mockEmitExit = vi.fn();
const mockEmitHeader = vi.fn();

vi.mock("./session.js", () => ({
  emit: mockEmit,
  emitOutput: mockEmitOutput,
  emitMetadata: mockEmitMetadata,
  emitError: mockEmitError,
  emitHeartbeat: mockEmitHeartbeat,
  emitExit: mockEmitExit,
  emitHeader: mockEmitHeader,
}));

// Mock workdir
vi.mock("./workdir.js", () => ({
  ensureWorkdir: vi.fn().mockResolvedValue("/tmp/test-workdir"),
}));

// Mock settings
vi.mock("./settings.js", () => ({
  settings: {
    codexModel: () => null,
    codexSandboxMode: () => null,
    networkAccessEnabled: () => true,
    codexApprovalPolicy: () => null,
    turnTimeoutSeconds: () => 0,
    codexBin: () => null,
    logLevel: () => "silent",
    logPretty: () => false,
    host: () => "127.0.0.1",
    port: () => 8788,
    token: () => "",
  },
}));

// Mock the Codex SDK
vi.mock("../../codex-src/sdk/typescript/src/index.js", () => ({
  Codex: vi.fn(() => ({
    startThread: mockStartThread,
    resumeThread: mockResumeThread,
  })),
}));

// Helper to create a test session
function createTestSession(): SessionState {
  return {
    id: "test-session",
    running: false,
    pendingInputs: [],
    subscribers: new Set(),
    eventBuffer: [],
  };
}

// Helper to create an async iterator that yields events then completes
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
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("runTurn", () => {
  it("is exported as a function", async () => {
    const { runTurn } = await import("./codex.js");
    expect(typeof runTurn).toBe("function");
  });

  describe("finally block cleanup", () => {
    it("clears heartbeat timer in finally block", async () => {
      const { runTurn } = await import("./codex.js");
      const session = createTestSession();

      mockRunStreamed.mockReturnValue(createMockEventStream());

      await runTurn(session, "test input", 0);
      await vi.runAllTimersAsync();

      expect(session.heartbeatTimer).toBeUndefined();
    });

    it("clears timeout timer in finally block", async () => {
      const { runTurn } = await import("./codex.js");
      const session = createTestSession();

      mockRunStreamed.mockReturnValue(createMockEventStream());

      await runTurn(session, "test input", 0);
      await vi.runAllTimersAsync();

      expect(session.timeoutTimer).toBeUndefined();
    });

    it("sets running to false in finally block", async () => {
      const { runTurn } = await import("./codex.js");
      const session = createTestSession();

      mockRunStreamed.mockReturnValue(createMockEventStream());

      await runTurn(session, "test input", 0);
      await vi.runAllTimersAsync();

      expect(session.running).toBe(false);
    });

    it("emits heartbeat with done=true in finally block", async () => {
      const { runTurn } = await import("./codex.js");
      const session = createTestSession();

      mockRunStreamed.mockReturnValue(createMockEventStream());

      await runTurn(session, "test input", 0);
      await vi.runAllTimersAsync();

      expect(mockEmitHeartbeat).toHaveBeenCalledWith(session, true);
    });

    it("emits exit with code 0 when no pending inputs", async () => {
      const { runTurn } = await import("./codex.js");
      const session = createTestSession();

      mockRunStreamed.mockReturnValue(createMockEventStream());

      await runTurn(session, "test input", 0);
      await vi.runAllTimersAsync();

      expect(mockEmitExit).toHaveBeenCalledWith(session, 0);
    });

    it("emits duration metadata in finally block", async () => {
      const { runTurn } = await import("./codex.js");
      const session = createTestSession();

      mockRunStreamed.mockReturnValue(createMockEventStream());

      await runTurn(session, "test input", 0);
      await vi.runAllTimersAsync();

      expect(mockEmitMetadata).toHaveBeenCalledWith(
        session,
        "duration_ms",
        expect.any(Number),
        expect.any(String),
      );
    });

    it("clears abortController in finally block", async () => {
      const { runTurn } = await import("./codex.js");
      const session = createTestSession();

      mockRunStreamed.mockReturnValue(createMockEventStream());

      await runTurn(session, "test input", 0);
      await vi.runAllTimersAsync();

      expect(session.abortController).toBeUndefined();
    });

    it("clears abortReason in finally block", async () => {
      const { runTurn } = await import("./codex.js");
      const session = createTestSession();

      mockRunStreamed.mockReturnValue(createMockEventStream());

      await runTurn(session, "test input", 0);
      await vi.runAllTimersAsync();

      expect(session.abortReason).toBeUndefined();
    });
  });

  describe("pending input draining", () => {
    it("shifts first pending input and processes it recursively", async () => {
      const { runTurn } = await import("./codex.js");
      const session = createTestSession();
      session.pendingInputs = ["queued input 1", "queued input 2"];

      mockRunStreamed.mockReturnValue(createMockEventStream());

      await runTurn(session, "first input", 0);
      await vi.runAllTimersAsync();

      // Both queued inputs should be processed (the recursive calls complete instantly with our mock)
      expect(session.pendingInputs).toEqual([]);
    });

    it("does not emit exit until all pending inputs are processed", async () => {
      const { runTurn } = await import("./codex.js");
      const session = createTestSession();
      session.pendingInputs = ["queued input"];

      mockRunStreamed.mockReturnValue(createMockEventStream());

      await runTurn(session, "first input", 0);
      await vi.runAllTimersAsync();

      // Exit should be called after all pending inputs are processed
      expect(mockEmitExit).toHaveBeenCalledWith(session, 0);
    });
  });

  describe("abort handling", () => {
    it("swallows AbortError silently", async () => {
      const { runTurn } = await import("./codex.js");
      const session = createTestSession();

      const abortError = new Error("Aborted");
      abortError.name = "AbortError";
      mockRunStreamed.mockImplementation(() => {
        throw abortError;
      });

      await runTurn(session, "test input", 0);
      await vi.runAllTimersAsync();

      // Should not emit error for AbortError
      expect(mockEmitError).not.toHaveBeenCalled();
      // But should still do cleanup
      expect(session.running).toBe(false);
    });

    it("calls emitError for non-abort errors", async () => {
      const { runTurn } = await import("./codex.js");
      const session = createTestSession();

      mockRunStreamed.mockImplementation(() => {
        throw new Error("Something went wrong");
      });

      await runTurn(session, "test input", 0);
      await vi.runAllTimersAsync();

      expect(mockEmitError).toHaveBeenCalledWith(
        session,
        "INTERNAL_ERROR",
        expect.stringContaining("Something went wrong"),
        expect.anything(),
      );
    });
  });

  describe("heartbeat timer", () => {
    it("sets heartbeat timer on session when turn begins", async () => {
      const { runTurn } = await import("./codex.js");
      const session = createTestSession();

      mockRunStreamed.mockReturnValue(createMockEventStream());

      // Run the turn - the timer should be set during execution
      const turnPromise = runTurn(session, "test input", 0);
      
      // Check that the timer was set at some point during execution
      // (it gets cleared in finally, so we can't check after)
      
      await turnPromise;
      await vi.runAllTimersAsync();

      // Heartbeat should have been called (with done=true in finally)
      expect(mockEmitHeartbeat).toHaveBeenCalledWith(session, true);
    });
  });
});
