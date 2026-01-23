/**
 * Session state management and SSE event emission.
 *
 * Sessions are stored in-memory since the agent (Python) owns persistence.
 * We only track the runtime state needed for active Codex SDK interactions.
 *
 * @module session
 */

import type { Response } from "express";
import type { SessionState } from "./types.js";
import { logger } from "./logger.js";

/**
 * In-memory registry of active sessions.
 *
 * Key: session_id from the agent
 * Value: Runtime state for that session
 */
const sessions = new Map<string, SessionState>();

/**
 * Get or create a session state object.
 *
 * Sessions are lazily created on first access. This allows the agent to
 * reference sessions before explicitly starting them (e.g., connecting
 * to the SSE stream before sending the first prompt).
 *
 * @param sessionId - The agent's session identifier
 * @returns The session state (existing or newly created)
 */
export function getSession(sessionId: string): SessionState {
  let session = sessions.get(sessionId);
  if (!session) {
    session = {
      id: sessionId,
      running: false,
      pendingInputs: [],
      subscribers: new Set(),
    };
    sessions.set(sessionId, session);
    logger.debug({ session_id: sessionId }, "Created new session state");
  }
  return session;
}

/**
 * Remove a session from the registry.
 *
 * Called during cleanup after a session is fully stopped.
 *
 * @param sessionId - The session to remove
 */
export function deleteSession(sessionId: string): void {
  sessions.delete(sessionId);
  logger.debug({ session_id: sessionId }, "Deleted session state");
}

// =============================================================================
// SSE Event Emission
// =============================================================================

/**
 * Emit a JSON event to all SSE subscribers of a session.
 *
 * Events are formatted as Server-Sent Events (SSE) with JSON payloads.
 * If no subscribers are connected, the event is silently dropped.
 *
 * @param session - The session to emit to
 * @param event - The event payload (will be JSON-serialized)
 */
export function emit(session: SessionState, event: unknown): void {
  const payload = `data: ${JSON.stringify(event)}\n\n`;
  for (const res of session.subscribers) {
    res.write(payload);
  }
}

/**
 * Emit an output event (agent text or step output).
 *
 * @param session - The session to emit to
 * @param text - The output text content
 * @param kind - "step" for intermediate output, "final" for agent responses
 */
export function emitOutput(session: SessionState, text: string, kind: "step" | "final"): void {
  emit(session, {
    type: "output",
    data: {
      stream: "combined",
      text,
      kind,
      final: kind === "final",
    },
  });
}

/**
 * Emit a metadata event (tokens, duration, etc.).
 *
 * @param session - The session to emit to
 * @param key - Metadata key (e.g., "tokens_used", "duration_ms")
 * @param value - The structured value
 * @param raw - Human-readable string representation
 */
export function emitMetadata(
  session: SessionState,
  key: string,
  value: unknown,
  raw: string,
): void {
  emit(session, {
    type: "metadata",
    data: { key, value, raw },
  });
}

/**
 * Emit an error event.
 *
 * Also logs the error for server-side visibility.
 *
 * @param session - The session to emit to
 * @param code - Error code (e.g., "TIMEOUT", "INTERNAL_ERROR")
 * @param message - Human-readable error message
 */
export function emitError(session: SessionState, code: string, message: string): void {
  logger.error({ session_id: session.id, code, message }, "Sidecar error");
  emit(session, { type: "error", data: { code, message } });
}

/**
 * Emit a heartbeat event.
 *
 * Heartbeats are sent periodically during turn execution to indicate
 * the session is still alive. The final heartbeat (done=true) signals
 * the turn has completed.
 *
 * @param session - The session to emit to
 * @param done - True if this is the final heartbeat for the turn
 */
export function emitHeartbeat(session: SessionState, done: boolean): void {
  const start = session.heartbeatStartMs ?? Date.now();
  const elapsedMs = Math.max(0, Date.now() - start);
  emit(session, {
    type: "heartbeat",
    data: { elapsed_s: elapsedMs / 1000, done },
  });
}

/**
 * Add an SSE subscriber to a session.
 *
 * @param session - The session to subscribe to
 * @param res - The Express response object for SSE streaming
 */
export function addSubscriber(session: SessionState, res: Response): void {
  session.subscribers.add(res);
  logger.info({ session_id: session.id }, "SSE client connected");
}

/**
 * Remove an SSE subscriber from a session.
 *
 * @param session - The session to unsubscribe from
 * @param res - The Express response object to remove
 */
export function removeSubscriber(session: SessionState, res: Response): void {
  session.subscribers.delete(res);
  logger.info({ session_id: session.id }, "SSE client disconnected");
}
