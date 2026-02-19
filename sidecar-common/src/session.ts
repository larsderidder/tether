/**
 * In-memory session registry and SSE emit helpers.
 *
 * Sessions are generic over the agent thread type T so each sidecar
 * can store its own SDK handle without casting.
 *
 * @module session
 */

import type { Response } from "express";
import type { SessionState } from "./types.js";
import type pino from "pino";

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

export function createSessionRegistry<T>() {
  const sessions = new Map<string, SessionState<T>>();

  function getSession(sessionId: string): SessionState<T> {
    let session = sessions.get(sessionId);
    if (!session) {
      session = {
        id: sessionId,
        running: false,
        pendingInputs: [],
        subscribers: new Set(),
        eventBuffer: [],
      };
      sessions.set(sessionId, session);
    }
    return session;
  }

  function deleteSession(sessionId: string): void {
    sessions.delete(sessionId);
  }

  return { getSession, deleteSession };
}

// ---------------------------------------------------------------------------
// SSE emit helpers
// ---------------------------------------------------------------------------

export function emit(session: SessionState<any>, event: unknown): void {
  if (session.subscribers.size === 0) {
    session.eventBuffer.push(event);
    return;
  }
  const payload = `data: ${JSON.stringify(event)}\n\n`;
  for (const res of session.subscribers) {
    res.write(payload);
  }
}

export function emitOutput(
  session: SessionState<any>,
  text: string,
  kind: "step" | "final",
): void {
  emit(session, {
    type: "output",
    data: { stream: "combined", text, kind, final: kind === "final" },
  });
}

export function emitMetadata(
  session: SessionState<any>,
  key: string,
  value: unknown,
  raw: string,
): void {
  emit(session, { type: "metadata", data: { key, value, raw } });
}

export function emitError(
  session: SessionState<any>,
  code: string,
  message: string,
  logger?: pino.Logger,
): void {
  logger?.error({ session_id: session.id, code, message }, "Sidecar error");
  emit(session, { type: "error", data: { code, message } });
}

export function emitHeartbeat(session: SessionState<any>, done: boolean): void {
  const start = session.heartbeatStartMs ?? Date.now();
  const elapsed_s = Math.max(0, Date.now() - start) / 1000;
  emit(session, { type: "heartbeat", data: { elapsed_s, done } });
}

export function emitExit(session: SessionState<any>, exitCode: number = 0): void {
  emit(session, { type: "exit", data: { exit_code: exitCode } });
}

export function emitHeader(
  session: SessionState<any>,
  title: string,
  opts: { model?: string; provider?: string; thread_id?: string } = {},
): void {
  emit(session, { type: "header", data: { title, ...opts } });
}

// ---------------------------------------------------------------------------
// Subscriber management
// ---------------------------------------------------------------------------

export function addSubscriber(
  session: SessionState<any>,
  res: Response,
  logger?: pino.Logger,
): void {
  session.subscribers.add(res);
  logger?.info({ session_id: session.id }, "SSE client connected");

  if (session.eventBuffer.length > 0) {
    logger?.info(
      { session_id: session.id, count: session.eventBuffer.length },
      "Replaying buffered events",
    );
    for (const event of session.eventBuffer) {
      res.write(`data: ${JSON.stringify(event)}\n\n`);
    }
    session.eventBuffer = [];
  }
}

export function removeSubscriber(
  session: SessionState<any>,
  res: Response,
  logger?: pino.Logger,
): void {
  session.subscribers.delete(res);
  logger?.info({ session_id: session.id }, "SSE client disconnected");
}
