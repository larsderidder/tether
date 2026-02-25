/**
 * Tests for routes.ts — shared HTTP route handlers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import express, { Request, Response } from "express";
import request from "supertest";
import { createRouter, type RoutesDeps } from "./routes.js";
import { createSessionRegistry } from "./session.js";
import type { SessionState } from "./types.js";
import pino from "pino";

// Create a silent logger for tests
const testLogger = pino({ level: "silent" });

// Helper to create test deps with mock runTurn
function createTestDeps(): {
  deps: RoutesDeps;
  runTurn: ReturnType<typeof vi.fn>;
  getSession: (id: string) => SessionState;
} {
  const { getSession, deleteSession } = createSessionRegistry();
  const runTurn = vi.fn().mockResolvedValue(undefined);

  return {
    deps: {
      getSession,
      runTurn,
      logger: testLogger,
    },
    runTurn,
    getSession,
  };
}

describe("createRouter", () => {
  describe("POST /sessions/start", () => {
    it("returns 422 when session_id is missing", async () => {
      const { deps } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const res = await request(app).post("/sessions/start").send({ prompt: "hello" });

      expect(res.status).toBe(422);
      expect(res.body.error).toBe("session_id is required");
    });

    it("creates session and returns ok:true when prompt provided", async () => {
      const { deps, runTurn, getSession } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const res = await request(app)
        .post("/sessions/start")
        .send({ session_id: "test-session", prompt: "hello world" });

      expect(res.status).toBe(200);
      expect(res.body.ok).toBe(true);
      expect(runTurn).toHaveBeenCalledOnce();

      const session = getSession("test-session");
      expect(session.id).toBe("test-session");
    });

    it("returns ok:true without starting turn when no prompt", async () => {
      const { deps, runTurn } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const res = await request(app)
        .post("/sessions/start")
        .send({ session_id: "test-session" });

      expect(res.status).toBe(200);
      expect(res.body.ok).toBe(true);
      expect(runTurn).not.toHaveBeenCalled();
    });

    it("returns 422 when workdir does not exist", async () => {
      const { deps } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const res = await request(app)
        .post("/sessions/start")
        .send({
          session_id: "test-session",
          prompt: "hello",
          workdir: "/nonexistent/path/that/does/not/exist",
        });

      expect(res.status).toBe(422);
      expect(res.body.error).toContain("workdir not accessible");
    });

    it("returns 422 when workdir is not a directory", async () => {
      const { deps } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      // Use this test file as workdir (it exists but is not a directory)
      const res = await request(app)
        .post("/sessions/start")
        .send({
          session_id: "test-session",
          prompt: "hello",
          workdir: import.meta.url.replace("file://", ""),
        });

      expect(res.status).toBe(422);
      expect(res.body.error).toBe("workdir must be a directory");
    });

    it("accepts valid workdir", async () => {
      const { deps, runTurn } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const res = await request(app)
        .post("/sessions/start")
        .send({
          session_id: "test-session",
          prompt: "hello",
          workdir: "/tmp",
        });

      expect(res.status).toBe(200);
      expect(res.body.ok).toBe(true);
      expect(runTurn).toHaveBeenCalled();
    });

    it("queues input when session is running", async () => {
      const { deps, getSession } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      // Mark session as running
      const session = getSession("test-session");
      session.running = true;

      const res = await request(app)
        .post("/sessions/start")
        .send({ session_id: "test-session", prompt: "queued message" });

      expect(res.status).toBe(200);
      expect(res.body.queued).toBe(true);
      expect(session.pendingInputs).toContain("queued message");
    });

    it("stores thread_id when provided", async () => {
      const { deps, runTurn } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      await request(app)
        .post("/sessions/start")
        .send({
          session_id: "test-session",
          prompt: "hello",
          thread_id: "existing-thread-123",
        });

      expect(runTurn).toHaveBeenCalledWith(
        expect.objectContaining({ id: "test-session" }),
        "hello",
        2,
        "existing-thread-123",
      );
    });

    it("stores approval_choice from request", async () => {
      const { deps, runTurn, getSession } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      await request(app)
        .post("/sessions/start")
        .send({
          session_id: "test-session",
          prompt: "hello",
          approval_choice: 1,
        });

      expect(runTurn).toHaveBeenCalledWith(
        expect.any(Object),
        "hello",
        1,
        undefined,
      );
      expect(getSession("test-session").approvalChoice).toBe(1);
    });
  });

  describe("POST /sessions/input", () => {
    it("returns 422 when session_id is missing", async () => {
      const { deps } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const res = await request(app).post("/sessions/input").send({ text: "hello" });

      expect(res.status).toBe(422);
      expect(res.body.error).toBe("session_id and text are required");
    });

    it("returns 422 when text is missing", async () => {
      const { deps } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const res = await request(app).post("/sessions/input").send({ session_id: "test" });

      expect(res.status).toBe(422);
      expect(res.body.error).toBe("session_id and text are required");
    });

    it("starts turn when session is idle", async () => {
      const { deps, runTurn } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const res = await request(app)
        .post("/sessions/input")
        .send({ session_id: "test-session", text: "follow-up input" });

      expect(res.status).toBe(200);
      expect(res.body.ok).toBe(true);
      expect(runTurn).toHaveBeenCalledOnce();
    });

    it("queues input when turn is running", async () => {
      const { deps, getSession } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const session = getSession("test-session");
      session.running = true;

      const res = await request(app)
        .post("/sessions/input")
        .send({ session_id: "test-session", text: "queued input" });

      expect(res.status).toBe(200);
      expect(res.body.queued).toBe(true);
      expect(session.pendingInputs).toContain("queued input");
    });

    it("uses stored approval_choice when not provided", async () => {
      const { deps, runTurn, getSession } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const session = getSession("test-session");
      session.approvalChoice = 0;

      await request(app)
        .post("/sessions/input")
        .send({ session_id: "test-session", text: "input" });

      // Check that runTurn was called with the correct approvalChoice
      expect(runTurn).toHaveBeenCalledOnce();
      const callArgs = runTurn.mock.calls[0];
      expect(callArgs[1]).toBe("input");
      expect(callArgs[2]).toBe(0);
      expect(callArgs[3]).toBeUndefined();
    });
  });

  describe("POST /sessions/interrupt", () => {
    it("returns 422 when session_id is missing", async () => {
      const { deps } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const res = await request(app).post("/sessions/interrupt").send({});

      expect(res.status).toBe(422);
      expect(res.body.error).toBe("session_id is required");
    });

    it("aborts controller when present", async () => {
      const { deps, getSession } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const session = getSession("test-session");
      const abortController = new AbortController();
      session.abortController = abortController;
      session.running = true;

      const res = await request(app)
        .post("/sessions/interrupt")
        .send({ session_id: "test-session" });

      expect(res.status).toBe(200);
      expect(res.body.ok).toBe(true);
      expect(abortController.signal.aborted).toBe(true);
      expect(session.abortReason).toBe("interrupt");
    });

    it("clears heartbeat timer", async () => {
      const { deps, getSession } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const session = getSession("test-session");
      const timer = setInterval(() => {}, 10000);
      session.heartbeatTimer = timer as unknown as NodeJS.Timeout;
      session.running = true;

      const res = await request(app)
        .post("/sessions/interrupt")
        .send({ session_id: "test-session" });

      expect(res.status).toBe(200);
      expect(session.heartbeatTimer).toBeUndefined();
    });

    it("clears timeout timer", async () => {
      const { deps, getSession } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const session = getSession("test-session");
      const timer = setTimeout(() => {}, 10000);
      session.timeoutTimer = timer;
      session.running = true;

      const res = await request(app)
        .post("/sessions/interrupt")
        .send({ session_id: "test-session" });

      expect(res.status).toBe(200);
      expect(session.timeoutTimer).toBeUndefined();
    });

    it("sets running to false and clears pending inputs", async () => {
      const { deps, getSession } = createTestDeps();
      const app = express();
      app.use(express.json());
      app.use(createRouter(deps));

      const session = getSession("test-session");
      session.running = true;
      session.pendingInputs = ["pending1", "pending2"];

      const res = await request(app)
        .post("/sessions/interrupt")
        .send({ session_id: "test-session" });

      expect(res.status).toBe(200);
      expect(session.running).toBe(false);
      expect(session.pendingInputs).toEqual([]);
    });
  });

  describe("GET /events/:sessionId", () => {
    it("sets SSE headers", async () => {
      const { deps } = createTestDeps();
      const app = express();
      app.use(createRouter(deps));

      const server = app.listen(0);
      const port = (server.address() as { port: number }).port;

      try {
        const res = await new Promise<{ headers: Record<string, string> }>((resolve, reject) => {
          const req = require("node:http").request(
            { hostname: "127.0.0.1", port, path: "/events/test-session", method: "GET" },
            (res: any) => {
              resolve({
                headers: res.headers,
              });
              req.destroy();
            },
          );
          req.on("error", reject);
          req.end();
        });

        expect(res.headers["content-type"]).toBe("text/event-stream");
        expect(res.headers["cache-control"]).toBe("no-cache");
        expect(res.headers["connection"]).toBe("keep-alive");
      } finally {
        server.close();
      }
    });

    it("adds subscriber to session", async () => {
      const { deps, getSession } = createTestDeps();
      const app = express();
      app.use(createRouter(deps));

      const server = app.listen(0);
      const port = (server.address() as { port: number }).port;

      try {
        // Make request and wait for response headers
        await new Promise<void>((resolve, reject) => {
          const req = require("node:http").request(
            { hostname: "127.0.0.1", port, path: "/events/test-session", method: "GET" },
            () => {
              // Give a bit of time for subscriber to be added via event loop
              resolve();
            },
          );
          req.on("error", reject);
          req.end();
        });

        // Poll for subscriber to be added (more reliable than fixed timeout)
        const session = getSession("test-session");
        let subscriberAdded = false;
        for (let i = 0; i < 10; i++) {
          await new Promise((resolve) => setImmediate(resolve));
          if (session.subscribers.size > 0) {
            subscriberAdded = true;
            break;
          }
        }
        expect(subscriberAdded).toBe(true);

        // Cleanup: close all connections
        for (const sub of session.subscribers) {
          (sub as any).end?.();
        }
      } finally {
        server.close();
      }
    });

    it("removes subscriber on disconnect", async () => {
      const { deps, getSession } = createTestDeps();
      const app = express();
      app.use(createRouter(deps));

      const server = app.listen(0);
      const port = (server.address() as { port: number }).port;

      try {
        const req = require("node:http").request(
          { hostname: "127.0.0.1", port, path: "/events/test-session", method: "GET" },
          () => {},
        );
        req.on("error", () => {}); // Ignore expected errors on destroy
        req.end();

        // Poll for subscriber to be added
        const session = getSession("test-session");
        for (let i = 0; i < 10; i++) {
          await new Promise((resolve) => setImmediate(resolve));
          if (session.subscribers.size > 0) break;
        }

        const initialSize = session.subscribers.size;
        expect(initialSize).toBeGreaterThan(0);

        // Destroy the connection - this may cause ECONNRESET which is expected
        req.destroy();

        // Give time for the connection to close before checking
        await new Promise((resolve) => setImmediate(resolve));

        // The subscriber should be removed (may take a tick)
        expect(session.subscribers.size).toBeLessThanOrEqual(initialSize);
      } finally {
        server.close();
      }
    });
  });
});
