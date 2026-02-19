/**
 * Codex SDK integration — runTurn implementation.
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
  emitHeader,
} from "./session.js";
import { ensureWorkdir } from "./workdir.js";
import { accessSync, constants as fsConstants } from "node:fs";

const HEARTBEAT_SECONDS = 5;
const LOG_EVENTS = process.env.TETHER_CODEX_SIDECAR_LOG_EVENTS === "1";

// ---------------------------------------------------------------------------
// Codex client
// ---------------------------------------------------------------------------

function resolveCodexBin(): string {
  const override = settings.codexBin();
  if (!override) return "codex";
  try {
    accessSync(override, fsConstants.X_OK);
    return override;
  } catch (err) {
    logger.warn(
      { override, error: err instanceof Error ? err.message : String(err) },
      "TETHER_CODEX_SIDECAR_CODEX_BIN is not executable; falling back to PATH",
    );
    return "codex";
  }
}

export const codex = new Codex({ codexPathOverride: resolveCodexBin() });

// ---------------------------------------------------------------------------
// Thread options
// ---------------------------------------------------------------------------

function buildThreadOptions(session: SessionState, approvalChoice: number): ThreadOptions {
  const options: ThreadOptions = { skipGitRepoCheck: true };

  if (session.workdir) options.workingDirectory = session.workdir;

  const model = settings.codexModel();
  if (model) options.model = model;

  const sandboxMode = settings.codexSandboxMode();
  if (sandboxMode) options.sandboxMode = sandboxMode as ThreadOptions["sandboxMode"];

  options.networkAccessEnabled = settings.networkAccessEnabled();

  const approvalPolicy = settings.codexApprovalPolicy();
  if (approvalPolicy) {
    options.approvalPolicy = approvalPolicy as ThreadOptions["approvalPolicy"];
  } else if (approvalChoice === 2) {
    options.approvalPolicy = "never";
  } else if (approvalChoice === 1) {
    options.approvalPolicy = "on-failure";
  } else {
    options.approvalPolicy = "on-request";
  }

  return options;
}

// ---------------------------------------------------------------------------
// Event handling
// ---------------------------------------------------------------------------

function formatStep(item: ThreadItem): string {
  switch (item.type) {
    case "reasoning":
      return (item as ReasoningItem).text;
    case "command_execution": {
      const cmd = (item as CommandExecutionItem).command;
      const exit = (item as CommandExecutionItem).exit_code;
      return `Command: ${cmd}${exit !== undefined ? ` (exit ${exit})` : ""}`;
    }
    case "file_change":
      return `File change: ${((item as FileChangeItem).changes || []).length} file(s)`;
    case "mcp_tool_call": {
      const m = item as McpToolCallItem;
      return `MCP: ${m.server}.${m.tool}`;
    }
    case "web_search":
      return `Web search: ${(item as WebSearchItem).query}`;
    case "todo_list": {
      const remaining = (item as TodoListItem).items.filter((t: { completed: boolean }) => !t.completed).length;
      return `Todo list: ${remaining} remaining`;
    }
    case "error":
      return `Error: ${(item as ErrorItem).message}`;
    default:
      return "";
  }
}

function handleEvent(session: SessionState, event: ThreadEvent, options: ThreadOptions): void {
  logger.debug({ session_id: session.id, event_type: event.type }, "SDK event");
  if (LOG_EVENTS) logger.debug({ session_id: session.id, event }, "SDK event details");

  switch (event.type) {
    case "thread.started":
      session.threadId = event.thread_id;
      emitHeader(session, "Codex SDK Sidecar", {
        model: options.model || "default",
        provider: "OpenAI (Codex)",
        thread_id: session.threadId,
      });
      break;

    case "item.completed": {
      const item = event.item;
      if (item.type === "agent_message") {
        const msg = (item as AgentMessageItem).text;
        emitOutput(session, msg.endsWith("\n") ? msg : `${msg}\n`, "final");
      } else {
        const text = formatStep(item);
        if (text) emitOutput(session, text.endsWith("\n") ? text : `${text}\n`, "step");
      }
      break;
    }

    case "turn.completed":
      emitUsage(session, event.usage);
      break;

    case "turn.failed":
      logger.error({ session_id: session.id, error: event.error }, "SDK turn failed");
      emitError(session, "INTERNAL_ERROR", event.error.message, logger);
      break;

    case "error":
      logger.error({ session_id: session.id, error: event.message }, "SDK error event");
      emitError(session, "INTERNAL_ERROR", event.message, logger);
      break;
  }
}

function emitUsage(session: SessionState, usage: Usage): void {
  const total = usage.input_tokens + usage.cached_input_tokens + usage.output_tokens;
  emitMetadata(session, "input_tokens", usage.input_tokens, String(usage.input_tokens));
  emitMetadata(session, "cached_input_tokens", usage.cached_input_tokens, String(usage.cached_input_tokens));
  emitMetadata(session, "output_tokens", usage.output_tokens, String(usage.output_tokens));
  emitMetadata(session, "tokens_used", total, String(total));
}

// ---------------------------------------------------------------------------
// runTurn
// ---------------------------------------------------------------------------

export async function runTurn(
  session: SessionState,
  input: string,
  approvalChoice: number,
  threadId?: string,
): Promise<void> {
  logger.debug({ session_id: session.id, input_length: input.length, approvalChoice }, "Starting turn");

  session.running = true;
  session.heartbeatStartMs = Date.now();
  session.abortReason = undefined;

  if (session.heartbeatTimer) clearInterval(session.heartbeatTimer);
  if (session.timeoutTimer) clearTimeout(session.timeoutTimer);

  session.heartbeatTimer = setInterval(() => emitHeartbeat(session, false), HEARTBEAT_SECONDS * 1000);

  try {
    if (!session.workdir && !threadId) {
      await ensureWorkdir(session, logger);
    }

    const options = buildThreadOptions(session, approvalChoice);

    if (!session.thread) {
      if (threadId) {
        logger.info({ session_id: session.id, thread_id: threadId }, "Resuming thread");
        session.thread = codex.resumeThread(threadId, options);
        session.threadId = threadId;
      } else {
        session.thread = codex.startThread(options);
      }
    }

    session.abortController = new AbortController();

    const turnTimeout = settings.turnTimeoutSeconds();
    if (turnTimeout > 0) {
      session.timeoutTimer = setTimeout(() => {
        emitError(session, "TIMEOUT", "Runner turn timed out", logger);
        session.abortReason = "timeout";
        session.abortController?.abort();
      }, turnTimeout * 1000);
    }

    const { events } = await session.thread.runStreamed(input, {
      signal: session.abortController.signal,
    });

    for await (const event of events) {
      handleEvent(session, event, options);
    }
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      logger.info({ session_id: session.id, reason: session.abortReason }, "Turn aborted");
    } else {
      logger.error({ session_id: session.id, error: err }, "Turn failed");

      if (
        err instanceof Error &&
        err.message.includes("spawn ") &&
        err.message.includes("codex") &&
        err.message.includes("ENOENT")
      ) {
        const override = settings.codexBin();
        const suffix = override ? ` (TETHER_CODEX_SIDECAR_CODEX_BIN=${override})` : "";
        emitError(
          session,
          "CODEX_NOT_FOUND",
          `Could not spawn 'codex' binary${suffix}: ${err.message}. Install Codex CLI or set TETHER_CODEX_SIDECAR_CODEX_BIN.`,
          logger,
        );
      } else {
        emitError(session, "INTERNAL_ERROR", String(err), logger);
      }
    }
  } finally {
    session.running = false;
    if (session.heartbeatTimer) { clearInterval(session.heartbeatTimer); session.heartbeatTimer = undefined; }
    if (session.timeoutTimer) { clearTimeout(session.timeoutTimer); session.timeoutTimer = undefined; }

    emitHeartbeat(session, true);
    session.abortController = undefined;
    session.abortReason = undefined;

    if (session.heartbeatStartMs) {
      const durationMs = Date.now() - session.heartbeatStartMs;
      emitMetadata(session, "duration_ms", durationMs, String(durationMs));
      session.heartbeatStartMs = undefined;
    }

    const next = session.pendingInputs.shift();
    if (next) {
      void runTurn(session, next, approvalChoice);
    } else {
      emitExit(session, 0);
    }
  }
}
