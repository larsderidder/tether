/**
 * OpenCode SDK integration — runTurn implementation.
 *
 * Each session gets its own opencode server instance scoped to the session's
 * working directory. The server is started lazily on the first turn and
 * reused for subsequent turns in the same session.
 *
 * Event mapping from OpenCode SDK to Tether SSE protocol:
 *   message.part.delta  (field=text)  -> emitOutput(..., "text chunk", kind="step")
 *   message.part.updated (step-finish) -> emitOutput("", "final") + emitUsage
 *   session.idle / session.status=idle -> emitExit(0)
 *   session.error                      -> emitError
 *   permission.updated                 -> emitError (unsupported for now)
 *
 * @module opencode
 */

import { createOpencodeServer } from "@opencode-ai/sdk";
import { createOpencodeClient } from "@opencode-ai/sdk";
import type { SessionState } from "@tether/sidecar-common/types";
import {
  emitOutput,
  emitMetadata,
  emitError,
  emitHeartbeat,
  emitExit,
  emitHeader,
} from "./session.js";
import type { OpencodeServerHandle } from "./session.js";
import { settings } from "./settings.js";
import { logger } from "./logger.js";
import { ensureWorkdir } from "@tether/sidecar-common/workdir";


const HEARTBEAT_SECONDS = 5;

// ---------------------------------------------------------------------------
// Server lifecycle
// ---------------------------------------------------------------------------

/**
 * Get or create the opencode server for a session.
 *
 * One server process per working directory. If a session's workdir changes
 * (which shouldn't happen after start) the old server is closed first.
 */
async function ensureServer(
  session: SessionState<OpencodeServerHandle>,
): Promise<OpencodeServerHandle> {
  const workdir = session.workdir ?? (await ensureWorkdir(session, logger));

  if (session.thread) {
    // Reuse if same directory.
    if (session.thread.directory === workdir) return session.thread;
    // Directory changed — close old server.
    logger.warn(
      { session_id: session.id, old: session.thread.directory, new: workdir },
      "Workdir changed; restarting opencode server",
    );
    session.thread.close();
    session.thread = undefined;
  }

  logger.info({ session_id: session.id, workdir }, "Starting opencode server");

  const opencodeBin = settings.opencodeBin();

  // createOpencodeServer spawns `opencode serve` and waits until ready.
  const server = await createOpencodeServer({
    hostname: "127.0.0.1",
    // Use a random port so multiple sessions don't collide.
    port: await getFreePort(),
    ...(opencodeBin ? { config: {} } : {}),
  });

  // createOpencodeClient connects to the running server.
  const client = createOpencodeClient({ baseUrl: server.url });

  const handle: OpencodeServerHandle = {
    client,
    close: server.close,
    url: server.url,
    directory: workdir,
  };

  session.thread = handle;
  logger.info({ session_id: session.id, url: server.url }, "OpenCode server ready");
  return handle;
}

async function getFreePort(): Promise<number> {
  const { createServer } = await import("node:net");
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      srv.close(() => {
        if (addr && typeof addr === "object") resolve(addr.port);
        else reject(new Error("Could not get free port"));
      });
    });
  });
}

// ---------------------------------------------------------------------------
// OpenCode session management
// ---------------------------------------------------------------------------

async function ensureOpencodeSession(
  handle: OpencodeServerHandle,
  tetherId: string,
  threadId?: string,
): Promise<string> {
  // If caller is resuming an attached opencode session, use it directly.
  if (threadId) return threadId;

  // Create a new opencode session scoped to the directory.
  const result = await handle.client.session.create({
    query: { directory: handle.directory },
  });
  // @ts-ignore — SDK response shape varies by version
  const oc = result.data as { id: string };
  logger.info({ tether_id: tetherId, opencode_session: oc.id }, "Created opencode session");
  return oc.id;
}

// ---------------------------------------------------------------------------
// SSE event stream
// ---------------------------------------------------------------------------

async function streamEvents(
  handle: OpencodeServerHandle,
  ocSessionId: string,
  session: SessionState<OpencodeServerHandle>,
  signal: AbortSignal,
): Promise<void> {
  // Subscribe to the global event stream and filter by session ID.
  const result = await handle.client.global.event();

  for await (const envelope of result.stream) {
    if (signal.aborted) break;

    // @ts-ignore
    const payload = envelope?.payload ?? envelope;
    const type: string = payload?.type ?? "";
    const props: Record<string, any> = payload?.properties ?? {};

    // Filter to our session only.
    const eventSessionId =
      props.sessionID ??
      props.info?.sessionID ??
      props.part?.sessionID ??
      props.error?.sessionID;

    if (eventSessionId && eventSessionId !== ocSessionId) continue;

    handleEvent(session, type, props);

    // Stop consuming once the session goes idle.
    if (type === "session.idle" || (type === "session.status" && props.status?.type === "idle")) {
      break;
    }

    // Stop on terminal error.
    if (type === "session.error") break;
  }
}

function handleEvent(
  session: SessionState<OpencodeServerHandle>,
  type: string,
  props: Record<string, any>,
): void {
  switch (type) {
    case "message.part.delta": {
      if (props.field === "text" && props.delta) {
        emitOutput(session, props.delta, "step");
      }
      break;
    }

    case "message.part.updated": {
      const part = props.part ?? props;
      if (part.type === "step-finish") {
        // Emit token/cost metadata.
        const tokens = part.tokens ?? {};
        const cost = part.cost ?? 0;
        if (tokens.input || tokens.output) {
          emitMetadata(session, "tokens_input", tokens.input ?? 0, String(tokens.input ?? 0));
          emitMetadata(session, "tokens_output", tokens.output ?? 0, String(tokens.output ?? 0));
          emitMetadata(session, "tokens_total", tokens.total ?? 0, String(tokens.total ?? 0));
        }
        if (cost) emitMetadata(session, "cost", cost, String(cost));
        emitOutput(session, "", "final");
      }
      break;
    }

    case "message.updated": {
      const info = props.info ?? props;
      if (info.role === "assistant" && info.modelID) {
        emitHeader(session, "OpenCode", {
          model: info.modelID,
          provider: info.providerID,
        });
      }
      break;
    }

    case "session.status": {
      // busy = heartbeat, idle = done (handled in streamEvents loop)
      if (props.status?.type === "busy") {
        emitHeartbeat(session, false);
      }
      break;
    }

    case "session.error": {
      const error = props.error ?? {};
      const msg = error.data?.message ?? JSON.stringify(error);
      emitError(session, "SESSION_ERROR", msg, logger);
      break;
    }

    case "permission.updated": {
      // OpenCode permissions are not yet wired to Tether's approval flow.
      // Auto-approve with "once" so the session doesn't stall.
      logger.warn(
        { session_id: session.id, permission: props.id, title: props.title },
        "OpenCode permission request — auto-approving (not yet wired to Tether approvals)",
      );
      if (session.thread && props.id && props.sessionID) {
        session.thread.client.session
          // @ts-ignore
          .postSessionIdPermissionsPermissionId({
            path: { id: props.sessionID, permissionID: props.id },
            body: { response: "once" },
          })
          .catch((err: unknown) =>
            logger.error({ error: String(err) }, "Failed to auto-approve permission"),
          );
      }
      break;
    }
  }
}

// ---------------------------------------------------------------------------
// runTurn
// ---------------------------------------------------------------------------

export async function runTurn(
  session: SessionState<OpencodeServerHandle>,
  input: string,
  approvalChoice: number,
  threadId?: string,
): Promise<void> {
  logger.debug({ session_id: session.id, input_length: input.length }, "Starting turn");

  session.running = true;
  session.heartbeatStartMs = Date.now();
  session.abortReason = undefined;

  if (session.heartbeatTimer) clearInterval(session.heartbeatTimer);
  if (session.timeoutTimer) clearTimeout(session.timeoutTimer);

  session.heartbeatTimer = setInterval(
    () => emitHeartbeat(session, false),
    HEARTBEAT_SECONDS * 1000,
  );

  try {
    const handle = await ensureServer(session);

    // Resolve or create the opencode session for this tether session.
    const ocSessionId =
      session.threadId ?? (await ensureOpencodeSession(handle, session.id, threadId));
    session.threadId = ocSessionId;

    emitHeader(session, "OpenCode", { thread_id: ocSessionId });

    session.abortController = new AbortController();

    const turnTimeout = settings.turnTimeoutSeconds();
    if (turnTimeout > 0) {
      session.timeoutTimer = setTimeout(() => {
        emitError(session, "TIMEOUT", "Turn timed out", logger);
        session.abortReason = "timeout";
        session.abortController?.abort();
      }, turnTimeout * 1000);
    }

    // Send the prompt (fire-and-forget, returns 204).
    await handle.client.session.promptAsync({
      path: { id: ocSessionId },
      body: { parts: [{ type: "text", text: input }] },
    });

    // Consume the event stream until idle or error.
    await streamEvents(handle, ocSessionId, session, session.abortController.signal);
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      logger.info({ session_id: session.id, reason: session.abortReason }, "Turn aborted");
    } else {
      logger.error({ session_id: session.id, error: String(err) }, "Turn failed");
      emitError(session, "INTERNAL_ERROR", String(err), logger);
    }
  } finally {
    session.running = false;
    if (session.heartbeatTimer) {
      clearInterval(session.heartbeatTimer);
      session.heartbeatTimer = undefined;
    }
    if (session.timeoutTimer) {
      clearTimeout(session.timeoutTimer);
      session.timeoutTimer = undefined;
    }

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
