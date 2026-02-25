/**
 * Tests for session.ts — session registry and SSE emit helpers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  createSessionRegistry,
  emit,
  emitOutput,
  emitMetadata,
  emitError,
  emitHeartbeat,
  emitExit,
  emitHeader,
  addSubscriber,
  removeSubscriber,
} from "./session.js";
import type { SessionState } from "./types.js";
import type { Response } from "express";

// Helper to create a mock Response object
function createMockResponse(): Response {
  return {
    write: vi.fn(),
    end: vi.fn(),
    flushHeaders: vi.fn(),
    setHeader: vi.fn(),
  } as unknown as Response;
}

// Helper to create a session state
function createTestSession(): SessionState {
  return {
    id: "test-session",
    running: false,
    pendingInputs: [],
    subscribers: new Set(),
    eventBuffer: [],
  };
}

describe("createSessionRegistry", () => {
  it("creates new sessions on demand", () => {
    const { getSession } = createSessionRegistry();
    const session = getSession("new-session-id");
    expect(session.id).toBe("new-session-id");
    expect(session.running).toBe(false);
    expect(session.pendingInputs).toEqual([]);
    expect(session.subscribers).toBeInstanceOf(Set);
    expect(session.eventBuffer).toEqual([]);
  });

  it("returns the same session for the same id", () => {
    const { getSession } = createSessionRegistry();
    const session1 = getSession("same-id");
    const session2 = getSession("same-id");
    expect(session1).toBe(session2);
  });

  it("deletes sessions", () => {
    const { getSession, deleteSession } = createSessionRegistry();
    getSession("to-delete");
    deleteSession("to-delete");
    const session = getSession("to-delete");
    // Should be a new session (old one was deleted)
    expect(session.running).toBe(false);
    expect(session.eventBuffer).toEqual([]);
  });
});

describe("emit", () => {
  it("buffers events when no subscribers connected", () => {
    const session = createTestSession();
    emit(session, { type: "test", data: "hello" });
    emit(session, { type: "test", data: "world" });

    expect(session.eventBuffer).toHaveLength(2);
    expect(session.eventBuffer[0]).toEqual({ type: "test", data: "hello" });
    expect(session.eventBuffer[1]).toEqual({ type: "test", data: "world" });
  });

  it("writes to all subscribers when connected", () => {
    const session = createTestSession();
    const res1 = createMockResponse();
    const res2 = createMockResponse();

    session.subscribers.add(res1);
    session.subscribers.add(res2);

    emit(session, { type: "test", data: "hello" });

    expect(res1.write).toHaveBeenCalledWith('data: {"type":"test","data":"hello"}\n\n');
    expect(res2.write).toHaveBeenCalledWith('data: {"type":"test","data":"hello"}\n\n');
    expect(session.eventBuffer).toHaveLength(0);
  });

  it("fan-out to multiple subscribers", () => {
    const session = createTestSession();
    const subscribers = [createMockResponse(), createMockResponse(), createMockResponse()];

    subscribers.forEach((res) => session.subscribers.add(res));

    emit(session, { type: "output", data: { text: "chunk" } });

    subscribers.forEach((res) => {
      expect(res.write).toHaveBeenCalledTimes(1);
    });
  });
});

describe("addSubscriber", () => {
  it("adds subscriber to session", () => {
    const session = createTestSession();
    const res = createMockResponse();

    addSubscriber(session, res);

    expect(session.subscribers.has(res)).toBe(true);
  });

  it("replays buffered events on connect", () => {
    const session = createTestSession();
    const res = createMockResponse();

    // Pre-buffer some events
    session.eventBuffer.push({ type: "test1", data: "a" });
    session.eventBuffer.push({ type: "test2", data: "b" });

    addSubscriber(session, res);

    expect(res.write).toHaveBeenCalledTimes(2);
    expect(res.write).toHaveBeenNthCalledWith(1, 'data: {"type":"test1","data":"a"}\n\n');
    expect(res.write).toHaveBeenNthCalledWith(2, 'data: {"type":"test2","data":"b"}\n\n');
    expect(session.eventBuffer).toHaveLength(0);
  });

  it("clears buffer after replay", () => {
    const session = createTestSession();
    const res = createMockResponse();

    session.eventBuffer.push({ type: "test", data: "x" });
    addSubscriber(session, res);

    expect(session.eventBuffer).toEqual([]);
  });
});

describe("removeSubscriber", () => {
  it("removes subscriber from session", () => {
    const session = createTestSession();
    const res = createMockResponse();

    session.subscribers.add(res);
    removeSubscriber(session, res);

    expect(session.subscribers.has(res)).toBe(false);
  });
});

describe("emitOutput", () => {
  it("emits output event with step kind", () => {
    const session = createTestSession();
    const res = createMockResponse();
    session.subscribers.add(res);

    emitOutput(session, "hello world", "step");

    const expectedPayload = {
      type: "output",
      data: { stream: "combined", text: "hello world", kind: "step", final: false },
    };
    expect(res.write).toHaveBeenCalledWith(`data: ${JSON.stringify(expectedPayload)}\n\n`);
  });

  it("emits output event with final kind", () => {
    const session = createTestSession();
    const res = createMockResponse();
    session.subscribers.add(res);

    emitOutput(session, "done", "final");

    const expectedPayload = {
      type: "output",
      data: { stream: "combined", text: "done", kind: "final", final: true },
    };
    expect(res.write).toHaveBeenCalledWith(`data: ${JSON.stringify(expectedPayload)}\n\n`);
  });
});

describe("emitMetadata", () => {
  it("emits metadata event", () => {
    const session = createTestSession();
    const res = createMockResponse();
    session.subscribers.add(res);

    emitMetadata(session, "tokens", 42, "42");

    const expectedPayload = {
      type: "metadata",
      data: { key: "tokens", value: 42, raw: "42" },
    };
    expect(res.write).toHaveBeenCalledWith(`data: ${JSON.stringify(expectedPayload)}\n\n`);
  });
});

describe("emitError", () => {
  it("emits error event", () => {
    const session = createTestSession();
    const res = createMockResponse();
    session.subscribers.add(res);

    emitError(session, "SESSION_ERROR", "Something went wrong");

    const expectedPayload = {
      type: "error",
      data: { code: "SESSION_ERROR", message: "Something went wrong" },
    };
    expect(res.write).toHaveBeenCalledWith(`data: ${JSON.stringify(expectedPayload)}\n\n`);
  });

  it("logs error when logger provided", () => {
    const session = createTestSession();
    const mockLogger = { error: vi.fn() };

    emitError(session, "CODE", "msg", mockLogger as unknown as { error: (args: unknown) => void });

    expect(mockLogger.error).toHaveBeenCalledWith(
      { session_id: session.id, code: "CODE", message: "msg" },
      "Sidecar error",
    );
  });
});

describe("emitHeartbeat", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("calculates elapsed time from heartbeatStartMs", () => {
    const session = createTestSession();
    const res = createMockResponse();
    session.subscribers.add(res);

    session.heartbeatStartMs = Date.now();
    vi.advanceTimersByTime(5000); // 5 seconds

    emitHeartbeat(session, false);

    const writeCall = (res.write as ReturnType<typeof vi.fn>).mock.calls[0][0];
    const payload = JSON.parse(writeCall.replace("data: ", "").replace("\n\n", ""));
    expect(payload.type).toBe("heartbeat");
    expect(payload.data.elapsed_s).toBeCloseTo(5, 1);
    expect(payload.data.done).toBe(false);
  });

  it("sets done flag to true when done", () => {
    const session = createTestSession();
    const res = createMockResponse();
    session.subscribers.add(res);

    session.heartbeatStartMs = Date.now();
    emitHeartbeat(session, true);

    const writeCall = (res.write as ReturnType<typeof vi.fn>).mock.calls[0][0];
    const payload = JSON.parse(writeCall.replace("data: ", "").replace("\n\n", ""));
    expect(payload.data.done).toBe(true);
  });

  it("uses current time as start if heartbeatStartMs not set", () => {
    const session = createTestSession();
    const res = createMockResponse();
    session.subscribers.add(res);

    // No heartbeatStartMs set, should use Date.now() and result in 0 elapsed
    emitHeartbeat(session, false);

    const writeCall = (res.write as ReturnType<typeof vi.fn>).mock.calls[0][0];
    const payload = JSON.parse(writeCall.replace("data: ", "").replace("\n\n", ""));
    expect(payload.data.elapsed_s).toBeGreaterThanOrEqual(0);
  });

  it("handles negative elapsed time gracefully", () => {
    const session = createTestSession();
    const res = createMockResponse();
    session.subscribers.add(res);

    // Set start time in the future (should result in 0 elapsed)
    session.heartbeatStartMs = Date.now() + 10000;
    emitHeartbeat(session, false);

    const writeCall = (res.write as ReturnType<typeof vi.fn>).mock.calls[0][0];
    const payload = JSON.parse(writeCall.replace("data: ", "").replace("\n\n", ""));
    expect(payload.data.elapsed_s).toBeGreaterThanOrEqual(0);
  });
});

describe("emitExit", () => {
  it("emits exit event with code", () => {
    const session = createTestSession();
    const res = createMockResponse();
    session.subscribers.add(res);

    emitExit(session, 0);

    const expectedPayload = { type: "exit", data: { exit_code: 0 } };
    expect(res.write).toHaveBeenCalledWith(`data: ${JSON.stringify(expectedPayload)}\n\n`);
  });

  it("emits exit event with non-zero code", () => {
    const session = createTestSession();
    const res = createMockResponse();
    session.subscribers.add(res);

    emitExit(session, 1);

    const expectedPayload = { type: "exit", data: { exit_code: 1 } };
    expect(res.write).toHaveBeenCalledWith(`data: ${JSON.stringify(expectedPayload)}\n\n`);
  });

  it("defaults exit code to 0", () => {
    const session = createTestSession();
    const res = createMockResponse();
    session.subscribers.add(res);

    emitExit(session);

    const expectedPayload = { type: "exit", data: { exit_code: 0 } };
    expect(res.write).toHaveBeenCalledWith(`data: ${JSON.stringify(expectedPayload)}\n\n`);
  });
});

describe("emitHeader", () => {
  it("emits header event with title only", () => {
    const session = createTestSession();
    const res = createMockResponse();
    session.subscribers.add(res);

    emitHeader(session, "Codex Sidecar");

    const expectedPayload = {
      type: "header",
      data: { title: "Codex Sidecar" },
    };
    expect(res.write).toHaveBeenCalledWith(`data: ${JSON.stringify(expectedPayload)}\n\n`);
  });

  it("emits header event with optional fields", () => {
    const session = createTestSession();
    const res = createMockResponse();
    session.subscribers.add(res);

    emitHeader(session, "OpenCode", { model: "gpt-4", provider: "OpenAI", thread_id: "thread-123" });

    const expectedPayload = {
      type: "header",
      data: { title: "OpenCode", model: "gpt-4", provider: "OpenAI", thread_id: "thread-123" },
    };
    expect(res.write).toHaveBeenCalledWith(`data: ${JSON.stringify(expectedPayload)}\n\n`);
  });
});
