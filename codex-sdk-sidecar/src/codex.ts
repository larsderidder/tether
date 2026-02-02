/**
 * Codex SDK integration.
 *
 * This module handles all interaction with the Codex SDK:
 * - Building thread options from settings
 * - Running conversation turns
 * - Processing SDK events and converting to our SSE format
 *
 * @module codex
 */

import { Codex } from "../../codex-src/sdk/typescript/src/index.js";
import type {
  ThreadEvent,
  ThreadOptions,
  ThreadItem,
  AgentMessageItem,
  ReasoningItem,
  CommandExecutionItem,
  FileChangeItem,
  McpToolCallItem,
  WebSearchItem,
  TodoListItem,
  ErrorItem,
  Usage,
} from "../../codex-src/sdk/typescript/src/index.js";
import type { SessionState } from "./types.js";
import { settings } from "./settings.js";
import { logger } from "./logger.js";
import {
  emit,
  emitOutput,
  emitMetadata,
  emitError,
  emitHeartbeat,
  emitExit,
} from "./session.js";
import { ensureWorkdir } from "./workdir.js";

// =============================================================================
// Constants
// =============================================================================

/** Interval between heartbeat events during a turn (seconds). */
const HEARTBEAT_SECONDS = 5;

/** Enable detailed SDK event logging (for debugging). Set TETHER_CODEX_SIDECAR_LOG_EVENTS=1 */
const LOG_EVENTS = process.env.TETHER_CODEX_SIDECAR_LOG_EVENTS === "1";

// =============================================================================
// Codex Client
// =============================================================================

/**
 * Singleton Codex SDK client instance.
 *
 * Created once at module load with optional binary path override.
 */
export const codex = new Codex({
  codexPathOverride: settings.codexBin(),
});

// =============================================================================
// Thread Options
// =============================================================================

/**
 * Build Codex ThreadOptions from current settings and session state.
 *
 * Merges environment configuration with per-request approval choice.
 *
 * @param session - Session state (for workdir)
 * @param approvalChoice - UI approval policy hint (1=auto, 2=ask)
 * @returns ThreadOptions for starting or continuing a thread
 */
export function buildThreadOptions(session: SessionState, approvalChoice: number): ThreadOptions {
  const options: ThreadOptions = {
    workingDirectory: session.workdir,
    skipGitRepoCheck: true, // Agent manages git context
  };

  // Apply settings from environment
  const model = settings.codexModel();
  if (model) {
    options.model = model;
  }

  const sandboxMode = settings.codexSandboxMode();
  if (sandboxMode) {
    options.sandboxMode = sandboxMode as ThreadOptions["sandboxMode"];
  }

  // Map approval_choice to Codex approvalPolicy
  // Match Claude's interpretation: 2 = full auto (no approvals), 1 = partial, 0 = ask
  const approvalPolicy = settings.codexApprovalPolicy();
  if (approvalPolicy) {
    options.approvalPolicy = approvalPolicy as ThreadOptions["approvalPolicy"];
  } else if (approvalChoice === 2) {
    // Full auto - never ask for approval (matches Claude's bypassPermissions)
    options.approvalPolicy = "never";
  } else if (approvalChoice === 1) {
    // Partial auto - ask on failure (matches Claude's acceptEdits)
    options.approvalPolicy = "on-failure";
  } else {
    // Default/0 - ask for approval
    options.approvalPolicy = "on-request";
  }

  return options;
}

// =============================================================================
// Header Emission
// =============================================================================

/**
 * Emit a session header with configuration summary.
 *
 * Sent at the start of each thread to show the user what's configured.
 *
 * @param session - The session to emit to
 * @param options - The thread options being used
 */
export function emitHeader(session: SessionState, options: ThreadOptions): void {
  emit(session, {
    type: "header",
    data: {
      title: "Codex SDK Sidecar",
      model: options.model || "default",
      provider: "OpenAI (Codex)",
      sandbox: options.sandboxMode || "default",
      approval: options.approvalPolicy || "default",
      session_id: session.id,
      thread_id: session.threadId ?? "unknown",
    },
  });
}

// =============================================================================
// Usage Reporting
// =============================================================================

/**
 * Emit token usage metadata.
 *
 * Reports input, cached input, output, and total tokens as separate
 * metadata events for the UI to display.
 *
 * @param session - The session to emit to
 * @param usage - Token usage from the SDK
 */
export function emitUsage(session: SessionState, usage: Usage): void {
  const total = usage.input_tokens + usage.cached_input_tokens + usage.output_tokens;

  emitMetadata(session, "input_tokens", usage.input_tokens, String(usage.input_tokens));
  emitMetadata(
    session,
    "cached_input_tokens",
    usage.cached_input_tokens,
    String(usage.cached_input_tokens),
  );
  emitMetadata(session, "output_tokens", usage.output_tokens, String(usage.output_tokens));
  emitMetadata(session, "tokens_used", total, String(total));
}

// =============================================================================
// Step Formatting
// =============================================================================

/**
 * Format a thread item as human-readable step text.
 *
 * Different item types are formatted differently:
 * - reasoning: Show the reasoning text
 * - command_execution: Show command and exit code
 * - file_change: Show number of files changed
 * - mcp_tool_call: Show server and tool name
 * - web_search: Show the search query
 * - todo_list: Show remaining items count
 * - error: Show the error message
 *
 * @param item - The thread item to format
 * @returns Formatted string, or empty string if not displayable
 */
function formatStep(item: ThreadItem): string {
  switch (item.type) {
    case "reasoning":
      return (item as ReasoningItem).text;

    case "command_execution": {
      const cmd = (item as CommandExecutionItem).command;
      const exit = (item as CommandExecutionItem).exit_code;
      const suffix = exit !== undefined ? ` (exit ${exit})` : "";
      return `Command: ${cmd}${suffix}`;
    }

    case "file_change": {
      const changes = (item as FileChangeItem).changes || [];
      return `File change: ${changes.length} file(s)`;
    }

    case "mcp_tool_call": {
      const mcp = item as McpToolCallItem;
      return `MCP: ${mcp.server}.${mcp.tool}`;
    }

    case "web_search": {
      const web = item as WebSearchItem;
      return `Web search: ${web.query}`;
    }

    case "todo_list": {
      const todo = item as TodoListItem;
      const remaining = todo.items.filter((t) => !t.completed).length;
      return `Todo list: ${remaining} remaining`;
    }

    case "error": {
      const err = item as ErrorItem;
      return `Error: ${err.message}`;
    }

    default:
      return "";
  }
}

/**
 * Emit a step event for a thread item.
 *
 * Formats the item and emits it as a "step" output event.
 * Items that don't have a displayable format are silently skipped.
 *
 * @param session - The session to emit to
 * @param item - The thread item to emit
 */
function emitStepForItem(session: SessionState, item: ThreadItem): void {
  if (!item) {
    return;
  }

  const text = formatStep(item);
  if (!text) {
    return;
  }

  // Ensure text ends with newline for consistent formatting
  emitOutput(session, text.endsWith("\n") ? text : `${text}\n`, "step");
}

// =============================================================================
// Event Handling
// =============================================================================

/**
 * Handle a Codex SDK thread event.
 *
 * Converts SDK events to our SSE event format:
 * - thread.started: Emit header with thread info
 * - item.completed: Emit as step or final output
 * - turn.completed: Emit token usage
 * - turn.failed/error: Emit error event
 *
 * @param session - The session receiving the event
 * @param event - The SDK event to handle
 * @param options - Thread options (for header emission)
 */
export function handleEvent(
  session: SessionState,
  event: ThreadEvent,
  options: ThreadOptions,
): void {
  // Always log event type at debug level
  logger.debug({ session_id: session.id, event_type: event.type }, "SDK event received");

  // Detailed event logging when enabled
  if (LOG_EVENTS) {
    logger.debug({ session_id: session.id, event }, "SDK event details");
  }

  switch (event.type) {
    case "thread.started":
      // Capture thread ID and emit header
      session.threadId = event.thread_id;
      emitHeader(session, options);
      break;

    case "item.completed": {
      const item = event.item;
      if (item.type === "agent_message") {
        // Agent messages are "final" output
        const msg = (item as AgentMessageItem).text;
        emitOutput(session, msg.endsWith("\n") ? msg : `${msg}\n`, "final");
      } else {
        // Everything else is a "step"
        emitStepForItem(session, item);
      }
      break;
    }

    case "turn.completed":
      // Report token usage
      emitUsage(session, event.usage);
      break;

    case "turn.failed":
      logger.error({ session_id: session.id, error: event.error }, "SDK turn failed");
      emitError(session, "INTERNAL_ERROR", event.error.message);
      break;

    case "error":
      logger.error({ session_id: session.id, error: event.message }, "SDK error event");
      emitError(session, "INTERNAL_ERROR", event.message);
      break;

    default:
      // Ignore unknown event types
      break;
  }
}

// =============================================================================
// Turn Execution
// =============================================================================

/**
 * Run a single conversation turn.
 *
 * A "turn" is one user input → agent response cycle. This function:
 * 1. Sets up heartbeat and timeout timers
 * 2. Ensures a working directory exists
 * 3. Creates or reuses the Codex thread
 * 4. Streams SDK events to SSE subscribers
 * 5. Processes any queued inputs after completion
 *
 * @param session - The session to run in
 * @param input - The user's input text
 * @param approvalChoice - UI approval policy hint
 */
export async function runTurn(
  session: SessionState,
  input: string,
  approvalChoice: number,
  threadId?: string,
): Promise<void> {
  logger.debug(
    { session_id: session.id, input_length: input.length, approvalChoice },
    "Starting turn",
  );

  // Mark session as running
  session.running = true;
  session.heartbeatStartMs = Date.now();
  session.abortReason = undefined;

  // Clear any existing timers
  if (session.heartbeatTimer) {
    clearInterval(session.heartbeatTimer);
  }
  if (session.timeoutTimer) {
    clearTimeout(session.timeoutTimer);
  }

  // Start heartbeat timer
  session.heartbeatTimer = setInterval(() => {
    emitHeartbeat(session, false);
  }, HEARTBEAT_SECONDS * 1000);

  try {
    // Ensure working directory exists
    const workdir = await ensureWorkdir(session);
    logger.debug({ session_id: session.id, workdir }, "Workdir resolved");

    const options = buildThreadOptions({ ...session, workdir }, approvalChoice);
    logger.debug({ session_id: session.id, options }, "Thread options built");

    // Create or resume thread if this is the first turn
    if (!session.thread) {
      if (threadId) {
        logger.info({ session_id: session.id, thread_id: threadId }, "Resuming existing thread");
        session.thread = codex.resumeThread(threadId, options);
        session.threadId = threadId; // Pre-populate so header shows correct ID
      } else {
        logger.debug({ session_id: session.id }, "Creating new thread");
        session.thread = codex.startThread(options);
      }
    }

    // Set up abort controller for stop/timeout
    session.abortController = new AbortController();

    // Set up turn timeout if configured
    const turnTimeout = settings.turnTimeoutSeconds();
    if (turnTimeout > 0) {
      session.timeoutTimer = setTimeout(() => {
        emitError(session, "TIMEOUT", "Runner turn timed out");
        session.abortReason = "timeout";
        session.abortController?.abort();
      }, turnTimeout * 1000);
    }

    // Run the turn and stream events
    logger.debug({ session_id: session.id }, "Calling runStreamed");
    const { events } = await session.thread.runStreamed(input, {
      signal: session.abortController.signal,
    });
    logger.debug({ session_id: session.id }, "runStreamed returned, iterating events");

    for await (const event of events) {
      handleEvent(session, event, options);
    }
    logger.debug({ session_id: session.id }, "Turn completed successfully");
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      // Expected when stop() or timeout triggers abort
      const reason = session.abortReason || "unknown";
      logger.info({ session_id: session.id, reason }, "Turn aborted");
    } else {
      // Unexpected error
      logger.error(
        {
          session_id: session.id,
          error:
            err instanceof Error
              ? { name: err.name, message: err.message, stack: err.stack, cause: err.cause }
              : { message: String(err) },
        },
        "Turn failed",
      );
      emitError(session, "INTERNAL_ERROR", String(err));
    }
  } finally {
    // Clean up timers
    session.running = false;
    if (session.heartbeatTimer) {
      clearInterval(session.heartbeatTimer);
      session.heartbeatTimer = undefined;
    }
    if (session.timeoutTimer) {
      clearTimeout(session.timeoutTimer);
      session.timeoutTimer = undefined;
    }

    // Emit final heartbeat
    emitHeartbeat(session, true);
    session.abortController = undefined;
    session.abortReason = undefined;

    // Emit turn duration
    if (session.heartbeatStartMs) {
      const durationMs = Date.now() - session.heartbeatStartMs;
      emitMetadata(session, "duration_ms", durationMs, String(durationMs));
      session.heartbeatStartMs = undefined;
    }

    // Process next queued input if any
    const next = session.pendingInputs.shift();
    if (next) {
      void runTurn(session, next, approvalChoice);
    } else {
      // No more inputs - emit exit to signal turn completion
      emitExit(session, 0);
    }
  }
}
