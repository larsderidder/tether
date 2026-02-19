/**
 * Shared HTTP route handlers for the sidecar API.
 *
 * Exposes a factory so each sidecar can inject its own runTurn
 * implementation and logger without importing them directly.
 *
 * Routes:
 *   POST /sessions/start      - Start a session or send first prompt
 *   POST /sessions/input      - Send follow-up input
 *   POST /sessions/interrupt  - Abort the running turn
 *   GET  /events/:sessionId   - SSE stream
 *
 * @module routes
 */

import { Router, Request, Response } from "express";
import { stat } from "node:fs/promises";
import type pino from "pino";
import type { RunTurn, SessionState } from "./types.js";
import { addSubscriber, removeSubscriber } from "./session.js";
import { setWorkdir } from "./workdir.js";

export type RoutesDeps = {
  getSession: (id: string) => SessionState<any>;
  runTurn: RunTurn;
  logger: pino.Logger;
};

export function createRouter(deps: RoutesDeps): Router {
  const { getSession, runTurn, logger } = deps;
  const router = Router();

  // POST /sessions/start
  router.post("/sessions/start", async (req: Request, res: Response) => {
    const { session_id, prompt, approval_choice, workdir, thread_id } = req.body || {};

    if (!session_id) {
      return res.status(422).json({ error: "session_id is required" });
    }

    const session = getSession(session_id);
    logger.info({ session_id }, "Start request");

    session.pendingInputs = [];

    if (session.running) {
      session.pendingInputs.push(String(prompt || ""));
      return res.json({ queued: true });
    }

    if (workdir) {
      try {
        const stats = await stat(String(workdir));
        if (!stats.isDirectory()) {
          return res.status(422).json({ error: "workdir must be a directory" });
        }
      } catch (err) {
        return res.status(422).json({ error: `workdir not accessible: ${String(err)}` });
      }
      await setWorkdir(session, String(workdir), logger);
    }

    session.approvalChoice = Number(approval_choice) || 2;

    if (prompt) {
      runTurn(session, String(prompt), session.approvalChoice, thread_id ? String(thread_id) : undefined).catch(
        (err) => logger.error({ session_id, error: String(err) }, "runTurn failed unexpectedly"),
      );
    }

    return res.json({ ok: true });
  });

  // POST /sessions/input
  router.post("/sessions/input", (req: Request, res: Response) => {
    const { session_id, text } = req.body || {};

    if (!session_id || !text) {
      return res.status(422).json({ error: "session_id and text are required" });
    }

    const session = getSession(session_id);
    logger.info({ session_id }, "Input request");

    if (session.running) {
      session.pendingInputs.push(String(text));
      return res.json({ queued: true });
    }

    const approvalChoice = session.approvalChoice ?? 2;
    runTurn(session, String(text), approvalChoice).catch((err) =>
      logger.error({ session_id, error: String(err) }, "runTurn failed unexpectedly"),
    );

    return res.json({ ok: true });
  });

  // POST /sessions/interrupt
  router.post("/sessions/interrupt", (req: Request, res: Response) => {
    const { session_id } = req.body || {};

    if (!session_id) {
      return res.status(422).json({ error: "session_id is required" });
    }

    const session = getSession(session_id);
    logger.info({ session_id }, "Interrupt request");

    if (session.abortController) {
      session.abortReason = "interrupt";
      session.abortController.abort();
    }

    if (session.heartbeatTimer) {
      clearInterval(session.heartbeatTimer);
      session.heartbeatTimer = undefined;
    }
    if (session.timeoutTimer) {
      clearTimeout(session.timeoutTimer);
      session.timeoutTimer = undefined;
    }

    session.running = false;
    session.pendingInputs = [];

    return res.json({ ok: true });
  });

  // GET /events/:sessionId
  router.get("/events/:sessionId", (req: Request, res: Response) => {
    const session = getSession(req.params["sessionId"] as string);

    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");
    res.flushHeaders();

    addSubscriber(session, res, logger);

    const keepalive = setInterval(() => res.write(": keepalive\n\n"), 30_000);

    req.on("close", () => {
      clearInterval(keepalive);
      removeSubscriber(session, res, logger);
    });
  });

  return router;
}
