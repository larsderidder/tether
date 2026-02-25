/**
 * Tests for server.ts — Express app factory.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import request from "supertest";
import { createSidecarApp } from "./server.js";
import { createSessionRegistry } from "./session.js";
import type pino from "pino";
import pino from "pino";

// Silent logger for tests
const testLogger = pino({ level: "silent" });

function createTestApp(options: {
  token?: string;
  runTurn?: () => Promise<void>;
} = {}) {
  const { getSession } = createSessionRegistry();
  const runTurn = options.runTurn ?? (async () => {});

  return createSidecarApp({
    logger: testLogger,
    token: options.token,
    getSession,
    runTurn,
    name: "test-sidecar",
  });
}

describe("createSidecarApp", () => {
  describe("GET /health", () => {
    it("returns health status", async () => {
      const app = createTestApp();

      const res = await request(app).get("/health");

      expect(res.status).toBe(200);
      expect(res.body.status).toBe("ok");
      expect(res.body.sidecar).toBe("test-sidecar");
    });
  });

  describe("auth middleware", () => {
    it("allows requests without token when token not configured", async () => {
      const app = createTestApp(); // No token

      const res = await request(app).get("/health");

      expect(res.status).toBe(200);
    });

    it("allows requests with correct token", async () => {
      const app = createTestApp({ token: "secret-token" });

      const res = await request(app)
        .get("/health")
        .set("x-sidecar-token", "secret-token");

      expect(res.status).toBe(200);
    });

    it("rejects requests with wrong token", async () => {
      const app = createTestApp({ token: "secret-token" });

      const res = await request(app)
        .get("/health")
        .set("x-sidecar-token", "wrong-token");

      expect(res.status).toBe(401);
      expect(res.body.error).toBe("unauthorized");
    });

    it("rejects requests without token when token is configured", async () => {
      const app = createTestApp({ token: "secret-token" });

      const res = await request(app).get("/health");

      expect(res.status).toBe(401);
      expect(res.body.error).toBe("unauthorized");
    });
  });

  describe("JSON body parsing", () => {
    it("parses JSON request bodies", async () => {
      const runTurn = vi.fn().mockResolvedValue(undefined);
      const app = createTestApp({ runTurn });

      const res = await request(app)
        .post("/sessions/start")
        .send({ session_id: "test", prompt: "hello" });

      expect(res.status).toBe(200);
      expect(runTurn).toHaveBeenCalled();
    });
  });
});
