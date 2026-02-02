/**
 * HTTP route handlers for the sidecar API.
 *
 * The sidecar exposes a simple REST API for the agent to control sessions:
 * - POST /sessions/start - Start a new session or send initial prompt
 * - POST /sessions/input - Send follow-up input to a running session
 * - POST /sessions/interrupt - Interrupt a running turn and clean up resources
 * - GET /events/:sessionId - SSE stream for receiving session events
 *
 * @module routes
 */

import { Router, Request, Response } from "express";
import { stat } from "node:fs/promises";
import { logger } from "./logger.js";
import { getSession, addSubscriber, removeSubscriber } from "./session.js";
import { setWorkdir, clearWorkdir } from "./workdir.js";
import { runTurn } from "./codex.js";

export const router = Router();

/**
 * POST /sessions/start
 *
 * Start a new Codex session or send an initial prompt.
 *
 * Request body:
 * - session_id (required): Unique session identifier from the agent
 * - prompt (optional): Initial prompt to send
 * - approval_choice (optional): 0=ask for approval, 1=partial auto, 2=full auto (no approvals)
 * - workdir (optional): Working directory for file operations
 *
 * If a turn is already running, the prompt is queued.
 *
 * Response:
 * - { ok: true } on success
 * - { queued: true } if the prompt was queued
 * - { error: string } on validation failure
 */
router.post("/sessions/start", async (req: Request, res: Response) => {
  const { session_id, prompt, approval_choice, workdir, thread_id } = req.body || {};

  // Validate required fields
  if (!session_id) {
    return res.status(422).json({ error: "session_id is required" });
  }

  const session = getSession(session_id);
  logger.info({ session_id }, "Start request");

  // Clear any previously queued inputs
  session.pendingInputs = [];

  // If already running, queue the input
  if (session.running) {
    session.pendingInputs.push(String(prompt || ""));
    return res.json({ queued: true });
  }

  // Handle workdir if provided
  if (workdir) {
    try {
      const stats = await stat(String(workdir));
      if (!stats.isDirectory()) {
        return res.status(422).json({ error: "workdir must be a directory" });
      }
    } catch (err) {
      return res.status(422).json({ error: `workdir not accessible: ${String(err)}` });
    }
    await setWorkdir(session, String(workdir));
  }

  // Store approval choice for follow-up inputs (default to 2 = full auto)
  session.approvalChoice = Number(approval_choice) || 2;

  // Start the turn if a prompt was provided
  if (prompt) {
    runTurn(session, String(prompt), session.approvalChoice, thread_id ? String(thread_id) : undefined).catch((err) => {
      logger.error({ session_id, error: String(err) }, "runTurn failed unexpectedly");
    });
  }

  return res.json({ ok: true });
});

/**
 * POST /sessions/input
 *
 * Send follow-up input to an existing session.
 *
 * Request body:
 * - session_id (required): Session to send input to
 * - text (required): The input text
 *
 * If a turn is running, the input is queued. Otherwise a new turn starts.
 *
 * Response:
 * - { ok: true } on success
 * - { queued: true } if the input was queued
 * - { error: string } on validation failure
 */
router.post("/sessions/input", (req: Request, res: Response) => {
  const { session_id, text } = req.body || {};

  // Validate required fields
  if (!session_id || !text) {
    return res.status(422).json({ error: "session_id and text are required" });
  }

  const session = getSession(session_id);
  logger.info({ session_id }, "Input request");

  // If already running, queue the input
  if (session.running) {
    session.pendingInputs.push(String(text));
    return res.json({ queued: true });
  }

  // Use stored approval choice from start(), fallback to 2 (full auto)
  const approvalChoice = session.approvalChoice ?? 2;

  // Start a new turn
  runTurn(session, String(text), approvalChoice).catch((err) => {
    logger.error({ session_id, error: String(err) }, "runTurn failed unexpectedly");
  });
  return res.json({ ok: true });
});

/**
 * POST /sessions/interrupt
 *
 * Interrupt a running turn. The session remains active for future input.
 *
 * Request body:
 * - session_id (required): Session to interrupt
 *
 * This will:
 * - Abort any running turn
 * - Clear timers and queued inputs
 * - Keep the thread intact (conversation can continue)
 *
 * Response:
 * - { ok: true } on success
 * - { error: string } on validation failure
 */
router.post("/sessions/interrupt", async (req: Request, res: Response) => {
  const { session_id } = req.body || {};

  // Validate required fields
  if (!session_id) {
    return res.status(422).json({ error: "session_id is required" });
  }

  const session = getSession(session_id);
  logger.info({ session_id }, "Interrupt request");

  // Abort any running turn
  if (session.abortController) {
    session.abortReason = "interrupt";
    session.abortController.abort();
  }

  // Clear timers
  if (session.heartbeatTimer) {
    clearInterval(session.heartbeatTimer);
    session.heartbeatTimer = undefined;
  }
  if (session.timeoutTimer) {
    clearTimeout(session.timeoutTimer);
    session.timeoutTimer = undefined;
  }

  // Reset turn state but keep thread for conversation continuity
  session.running = false;
  session.pendingInputs = [];

  return res.json({ ok: true });
});

/**
 * GET /events/:sessionId
 *
 * SSE endpoint for receiving session events.
 *
 * The agent connects to this endpoint to receive:
 * - output: Agent text and step output
 * - metadata: Token counts, duration, etc.
 * - heartbeat: Periodic liveness signals
 * - error: Error notifications
 * - header: Session configuration info
 *
 * The connection stays open until the client disconnects.
 * Multiple clients can connect to the same session.
 */
router.get("/events/:sessionId", (req: Request, res: Response) => {
  const session = getSession(req.params.sessionId);

  // Set up SSE headers
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.flushHeaders();

  // Register as subscriber
  addSubscriber(session, res);

  // Send keepalive comments every 30s to prevent read timeouts
  const keepaliveTimer = setInterval(() => {
    res.write(": keepalive\n\n");
  }, 30_000);

  // Clean up on disconnect
  req.on("close", () => {
    clearInterval(keepaliveTimer);
    removeSubscriber(session, res);
  });
});
