/**
 * Type definitions for the Codex SDK Sidecar.
 *
 * @module types
 */

import type { Response } from "express";
import type { Codex } from "../../codex-src/sdk/typescript/src/index.js";

/**
 * Represents the runtime state of a single agent session.
 *
 * Each session corresponds to one conversation thread with the Codex SDK.
 * The agent (Python) owns session persistence; we only track runtime state
 * needed for active execution.
 */
export type SessionState = {
  /** Unique session identifier (matches agent's session_id). */
  id: string;

  /** Active Codex thread handle, if a conversation has been started. */
  thread?: ReturnType<Codex["startThread"]>;

  /** Codex-assigned thread ID, captured from thread.started event. */
  threadId?: string;

  /** Controller to abort the current turn (for interrupt/timeout). */
  abortController?: AbortController;

  /** Reason for abort: user-initiated interrupt or timeout. */
  abortReason?: "interrupt" | "timeout";

  /** True while a turn is actively running. */
  running: boolean;

  /** Approval choice for this session (1=acceptEdits, 2=bypassPermissions). */
  approvalChoice?: number;

  /** Queue of user inputs received while a turn is in progress. */
  pendingInputs: string[];

  /** Working directory for this session's file operations. */
  workdir?: string;

  /** True if we created the workdir (and should clean it up). */
  workdirManaged?: boolean;

  /** Connected SSE clients waiting for events. */
  subscribers: Set<Response>;

  /** Buffered events emitted before any subscriber connected. */
  eventBuffer: unknown[];

  /** Interval timer for periodic heartbeat emissions. */
  heartbeatTimer?: NodeJS.Timeout;

  /** Timestamp (ms) when the current turn started, for elapsed time. */
  heartbeatStartMs?: number;

  /** Timeout timer for turn timeout enforcement. */
  timeoutTimer?: NodeJS.Timeout;
};

/**
 * SSE event payload for output text.
 */
export type OutputEvent = {
  type: "output";
  data: {
    stream: "combined";
    text: string;
    kind: "step" | "final";
    final: boolean;
  };
};

/**
 * SSE event payload for metadata (tokens, duration, etc.).
 */
export type MetadataEvent = {
  type: "metadata";
  data: {
    key: string;
    value: unknown;
    raw: string;
  };
};

/**
 * SSE event payload for errors.
 */
export type ErrorEvent = {
  type: "error";
  data: {
    code: string;
    message: string;
  };
};

/**
 * SSE event payload for heartbeats.
 */
export type HeartbeatEvent = {
  type: "heartbeat";
  data: {
    elapsed_s: number;
    done: boolean;
  };
};

/**
 * SSE event payload for session header info.
 */
export type HeaderEvent = {
  type: "header";
  data: {
    text: string;
  };
};

/**
 * Union of all SSE event types.
 */
export type SidecarEvent =
  | OutputEvent
  | MetadataEvent
  | ErrorEvent
  | HeartbeatEvent
  | HeaderEvent;
