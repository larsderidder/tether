/**
 * Tests for opencode.ts — OpenCode SDK integration.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { handleEvent, clearHiddenParts, clearSessionModel, clearLastEmittedPartId, getSessionModel } from "./opencode.js";
import type { SessionState } from "@tether/sidecar-common/types";
import type { OpencodeServerHandle } from "./session.js";

// Mock the emit functions from session.js
vi.mock("./session.js", () => ({
  emitOutput: vi.fn(),
  emitMetadata: vi.fn(),
  emitError: vi.fn(),
  emitHeartbeat: vi.fn(),
  emitExit: vi.fn(),
  emitHeader: vi.fn(),
}));

import {
  emitOutput,
  emitMetadata,
  emitError,
  emitHeartbeat,
  emitExit,
  emitHeader,
} from "./session.js";

// Helper to create a test session
function createTestSession(): SessionState<OpencodeServerHandle> {
  return {
    id: "test-session",
    running: false,
    pendingInputs: [],
    subscribers: new Set(),
    eventBuffer: [],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  clearHiddenParts("test-session");
  clearSessionModel("test-session");
  clearLastEmittedPartId("test-session");
});

describe("handleEvent", () => {
  describe("message.part.delta", () => {
    it("emits output when field is text and delta is present", () => {
      const session = createTestSession();

      handleEvent(session, "message.part.delta", {
        field: "text",
        delta: "Hello ",
      });

      expect(emitOutput).toHaveBeenCalledWith(session, "Hello ", "step");
    });

    it("does not emit when field is not text", () => {
      const session = createTestSession();

      handleEvent(session, "message.part.delta", {
        field: "other",
        delta: "some content",
      });

      expect(emitOutput).not.toHaveBeenCalled();
    });

    it("does not emit when delta is empty", () => {
      const session = createTestSession();

      handleEvent(session, "message.part.delta", {
        field: "text",
        delta: "",
      });

      expect(emitOutput).not.toHaveBeenCalled();
    });

    it("does not emit when delta is missing", () => {
      const session = createTestSession();

      handleEvent(session, "message.part.delta", {
        field: "text",
      });

      expect(emitOutput).not.toHaveBeenCalled();
    });

    it("suppresses deltas from reasoning parts", () => {
      const session = createTestSession();

      // Register the part as reasoning via message.part.updated.
      handleEvent(session, "message.part.updated", {
        part: { id: "part-reason-1", type: "reasoning", text: "" },
      });

      // Delta for that part should be suppressed.
      handleEvent(session, "message.part.delta", {
        partID: "part-reason-1",
        field: "text",
        delta: "Let me think about this...",
      });

      expect(emitOutput).not.toHaveBeenCalled();
    });

    it("allows deltas from text parts", () => {
      const session = createTestSession();

      // Register the part as text via message.part.updated.
      handleEvent(session, "message.part.updated", {
        part: { id: "part-text-1", type: "text", text: "" },
      });

      handleEvent(session, "message.part.delta", {
        partID: "part-text-1",
        field: "text",
        delta: "Hello!",
      });

      expect(emitOutput).toHaveBeenCalledWith(session, "Hello!", "step");
    });

    it("allows deltas when partID is not tracked", () => {
      const session = createTestSession();

      // No prior message.part.updated for this partID.
      handleEvent(session, "message.part.delta", {
        partID: "unknown-part",
        field: "text",
        delta: "Some text",
      });

      expect(emitOutput).toHaveBeenCalledWith(session, "Some text", "step");
    });

    it("inserts separator when switching to a new text part", () => {
      const session = createTestSession();

      handleEvent(session, "message.part.delta", {
        partID: "part-1",
        field: "text",
        delta: "Thinking...",
      });

      handleEvent(session, "message.part.delta", {
        partID: "part-2",
        field: "text",
        delta: "Hello!",
      });

      expect(emitOutput).toHaveBeenCalledTimes(3);
      expect(emitOutput).toHaveBeenNthCalledWith(1, session, "Thinking...", "step");
      expect(emitOutput).toHaveBeenNthCalledWith(2, session, "\n\n", "step");
      expect(emitOutput).toHaveBeenNthCalledWith(3, session, "Hello!", "step");
    });

    it("does not insert separator for same part", () => {
      const session = createTestSession();

      handleEvent(session, "message.part.delta", {
        partID: "part-1",
        field: "text",
        delta: "Hello ",
      });

      handleEvent(session, "message.part.delta", {
        partID: "part-1",
        field: "text",
        delta: "world",
      });

      expect(emitOutput).toHaveBeenCalledTimes(2);
      expect(emitOutput).toHaveBeenNthCalledWith(1, session, "Hello ", "step");
      expect(emitOutput).toHaveBeenNthCalledWith(2, session, "world", "step");
    });

    it("clears hidden parts on step-finish", () => {
      const session = createTestSession();

      // Register reasoning part.
      handleEvent(session, "message.part.updated", {
        part: { id: "part-reason-1", type: "reasoning", text: "" },
      });

      // step-finish clears the tracking.
      handleEvent(session, "message.part.updated", {
        part: { type: "step-finish" },
      });

      // A new reasoning part with the same ID should need re-registration.
      // But more importantly, the old ID is no longer tracked.
      handleEvent(session, "message.part.delta", {
        partID: "part-reason-1",
        field: "text",
        delta: "This should be emitted now",
      });

      // emitOutput was called twice: once for "" (final) from step-finish,
      // and once for the delta.
      expect(emitOutput).toHaveBeenCalledWith(session, "This should be emitted now", "step");
    });
  });

  describe("message.part.updated (step-finish)", () => {
    it("emits token metadata when tokens present", () => {
      const session = createTestSession();

      handleEvent(session, "message.part.updated", {
        part: {
          type: "step-finish",
          tokens: { input: 100, output: 50, total: 150 },
        },
      });

      expect(emitMetadata).toHaveBeenCalledWith(session, "tokens_input", 100, "100");
      expect(emitMetadata).toHaveBeenCalledWith(session, "tokens_output", 50, "50");
      expect(emitMetadata).toHaveBeenCalledWith(session, "tokens_total", 150, "150");
      expect(emitOutput).toHaveBeenCalledWith(session, "", "final");
    });

    it("emits cost metadata when cost present", () => {
      const session = createTestSession();

      handleEvent(session, "message.part.updated", {
        part: {
          type: "step-finish",
          cost: 0.05,
        },
      });

      expect(emitMetadata).toHaveBeenCalledWith(session, "cost", 0.05, "0.05");
      expect(emitOutput).toHaveBeenCalledWith(session, "", "final");
    });

    it("emits final output without tokens or cost", () => {
      const session = createTestSession();

      handleEvent(session, "message.part.updated", {
        part: {
          type: "step-finish",
        },
      });

      expect(emitOutput).toHaveBeenCalledWith(session, "", "final");
    });

    it("does not emit for non-step-finish types", () => {
      const session = createTestSession();

      handleEvent(session, "message.part.updated", {
        part: {
          type: "other",
        },
      });

      expect(emitOutput).not.toHaveBeenCalled();
      expect(emitMetadata).not.toHaveBeenCalled();
    });

    it("handles props directly without part wrapper", () => {
      const session = createTestSession();

      handleEvent(session, "message.part.updated", {
        type: "step-finish",
        tokens: { input: 10 },
      });

      expect(emitMetadata).toHaveBeenCalledWith(session, "tokens_input", 10, "10");
    });
  });

  describe("message.updated", () => {
    it("emits header for assistant messages with modelID", () => {
      const session = createTestSession();

      handleEvent(session, "message.updated", {
        info: {
          role: "assistant",
          modelID: "gpt-4",
          providerID: "openai",
        },
      });

      expect(emitHeader).toHaveBeenCalledWith(session, "OpenCode", {
        model: "gpt-4",
        provider: "openai",
      });
    });

    it("does not emit header for non-assistant roles", () => {
      const session = createTestSession();

      handleEvent(session, "message.updated", {
        info: {
          role: "user",
          modelID: "gpt-4",
        },
      });

      expect(emitHeader).not.toHaveBeenCalled();
    });

    it("does not emit header when modelID is missing", () => {
      const session = createTestSession();

      handleEvent(session, "message.updated", {
        info: {
          role: "assistant",
        },
      });

      expect(emitHeader).not.toHaveBeenCalled();
    });

    it("caches model from assistant message for subsequent turns", () => {
      const session = createTestSession();

      handleEvent(session, "message.updated", {
        info: {
          role: "assistant",
          modelID: "claude-sonnet-4-20250514",
          providerID: "anthropic",
        },
      });

      const model = getSessionModel("test-session");
      expect(model).toEqual({
        modelID: "claude-sonnet-4-20250514",
        providerID: "anthropic",
      });
    });
  });

  describe("session.status", () => {
    it("emits heartbeat when status is busy", () => {
      const session = createTestSession();

      handleEvent(session, "session.status", {
        status: { type: "busy" },
      });

      expect(emitHeartbeat).toHaveBeenCalledWith(session, false);
    });

    it("does not emit heartbeat when status is not busy", () => {
      const session = createTestSession();

      handleEvent(session, "session.status", {
        status: { type: "idle" },
      });

      expect(emitHeartbeat).not.toHaveBeenCalled();
    });
  });

  describe("session.error", () => {
    it("emits error with message from error.data.message", () => {
      const session = createTestSession();

      handleEvent(session, "session.error", {
        error: {
          data: { message: "Something went wrong" },
        },
      });

      expect(emitError).toHaveBeenCalledWith(
        session,
        "SESSION_ERROR",
        "Something went wrong",
        expect.anything(),
      );
    });

    it("emits error with stringified error when no message", () => {
      const session = createTestSession();

      handleEvent(session, "session.error", {
        error: { code: 500 },
      });

      expect(emitError).toHaveBeenCalledWith(
        session,
        "SESSION_ERROR",
        '{"code":500}',
        expect.anything(),
      );
    });
  });

  describe("permission.updated", () => {
    it("auto-approves permission when thread and permission info present", async () => {
      const session = createTestSession();
      const mockClient = {
        session: {
          postSessionIdPermissionsPermissionId: vi.fn().mockResolvedValue({}),
        },
      };
      session.thread = {
        client: mockClient as any,
        close: vi.fn(),
        url: "http://localhost:3000",
        directory: "/test/dir",
      };

      handleEvent(session, "permission.updated", {
        id: "perm-123",
        sessionID: "sess-456",
        title: "Allow file access?",
      });

      // Wait for async call
      await new Promise((resolve) => setTimeout(resolve, 10));

      expect(mockClient.session.postSessionIdPermissionsPermissionId).toHaveBeenCalledWith({
        path: { id: "sess-456", permissionID: "perm-123" },
        body: { response: "once" },
      });
    });

    it("does nothing when thread is not present", () => {
      const session = createTestSession();

      handleEvent(session, "permission.updated", {
        id: "perm-123",
        sessionID: "sess-456",
      });

      // Should not throw, no client call
    });

    it("does nothing when permission id is missing", () => {
      const session = createTestSession();
      const mockClient = {
        session: {
          postSessionIdPermissionsPermissionId: vi.fn(),
        },
      };
      session.thread = {
        client: mockClient as any,
        close: vi.fn(),
        url: "http://localhost:3000",
        directory: "/test/dir",
      };

      handleEvent(session, "permission.updated", {
        sessionID: "sess-456",
      });

      expect(mockClient.session.postSessionIdPermissionsPermissionId).not.toHaveBeenCalled();
    });
  });

  describe("unknown event type", () => {
    it("does not emit anything for unknown types", () => {
      const session = createTestSession();

      handleEvent(session, "unknown.event", { foo: "bar" });

      expect(emitOutput).not.toHaveBeenCalled();
      expect(emitMetadata).not.toHaveBeenCalled();
      expect(emitError).not.toHaveBeenCalled();
      expect(emitHeartbeat).not.toHaveBeenCalled();
      expect(emitExit).not.toHaveBeenCalled();
      expect(emitHeader).not.toHaveBeenCalled();
    });
  });
});

describe("runTurn", () => {
  // The runTurn function requires complex mocking of the SDK.
  // These tests verify the function structure and basic behavior.
  it("runTurn is exported as a function", async () => {
    const { runTurn } = await import("./opencode.js");
    expect(typeof runTurn).toBe("function");
  });
});
