/**
 * Shared type definitions for Tether sidecars.
 *
 * SessionState is generic over the agent-specific thread handle type T.
 * Each sidecar instantiates this with its own SDK thread type.
 *
 * @module types
 */

import type { Response } from "express";

/**
 * Runtime state for a single agent session.
 *
 * The Python agent owns persistence. The sidecar only tracks what it
 * needs to manage an active turn.
 */
export type SessionState<T = unknown> = {
  /** Unique session identifier (matches agent's session_id). */
  id: string;

  /** Agent-specific thread/conversation handle. */
  thread?: T;

  /** Agent-assigned thread/session ID, for resumption. */
  threadId?: string;

  /** AbortController for the current turn. */
  abortController?: AbortController;

  /** Reason the current turn was aborted. */
  abortReason?: "interrupt" | "timeout";

  /** True while a turn is actively running. */
  running: boolean;

  /** Approval choice stored from /sessions/start for re-use on /sessions/input. */
  approvalChoice?: number;

  /** Inputs queued while a turn is in progress. */
  pendingInputs: string[];

  /** Working directory for this session. */
  workdir?: string;

  /** True if this sidecar created the workdir and should clean it up. */
  workdirManaged?: boolean;

  /** Connected SSE response streams. */
  subscribers: Set<Response>;

  /** Events emitted before any subscriber connected, replayed on connect. */
  eventBuffer: unknown[];

  /** Interval timer for periodic heartbeat emissions. */
  heartbeatTimer?: NodeJS.Timeout;

  /** Timestamp (ms) when the current turn started. */
  heartbeatStartMs?: number;

  /** Timeout timer for enforcing turn time limits. */
  timeoutTimer?: NodeJS.Timeout;
};

// ---------------------------------------------------------------------------
// SSE event shapes (what the Python runner consumes)
// ---------------------------------------------------------------------------

export type OutputEvent = {
  type: "output";
  data: { stream: "combined"; text: string; kind: "step" | "final"; final: boolean };
};

export type MetadataEvent = {
  type: "metadata";
  data: { key: string; value: unknown; raw: string };
};

export type ErrorEvent = {
  type: "error";
  data: { code: string; message: string };
};

export type HeartbeatEvent = {
  type: "heartbeat";
  data: { elapsed_s: number; done: boolean };
};

export type HeaderEvent = {
  type: "header";
  data: { title: string; model?: string; provider?: string; thread_id?: string };
};

export type ExitEvent = {
  type: "exit";
  data: { exit_code: number };
};

export type SidecarEvent =
  | OutputEvent
  | MetadataEvent
  | ErrorEvent
  | HeartbeatEvent
  | HeaderEvent
  | ExitEvent;

// ---------------------------------------------------------------------------
// runTurn contract
// ---------------------------------------------------------------------------

/**
 * Signature every sidecar agent module must implement.
 *
 * Called by the shared route handlers whenever a new prompt arrives.
 */
export type RunTurn = (
  session: SessionState<any>,
  input: string,
  approvalChoice: number,
  threadId?: string,
) => Promise<void>;
