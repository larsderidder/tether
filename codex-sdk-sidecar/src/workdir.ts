/**
 * Working directory management for sessions.
 *
 * Each session can have its own working directory where Codex executes
 * file operations. Directories can be:
 * - Provided by the agent (managed externally, not cleaned up)
 * - Created by us as temp directories (cleaned up when session stops)
 *
 * @module workdir
 */

import { tmpdir } from "node:os";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import type { SessionState } from "./types.js";
import { logger } from "./logger.js";

/**
 * Ensure a session has a working directory.
 *
 * If the session already has a workdir, returns it. Otherwise creates
 * a new temporary directory that will be cleaned up when the session stops.
 *
 * @param session - The session needing a workdir
 * @returns Path to the working directory
 */
export async function ensureWorkdir(session: SessionState): Promise<string> {
  if (session.workdir) {
    return session.workdir;
  }

  // Create a temp directory with a recognizable prefix
  const dir = await mkdtemp(join(tmpdir(), `tether_${session.id}_`));
  session.workdir = dir;
  session.workdirManaged = true;

  logger.debug({ session_id: session.id, workdir: dir }, "Created temp workdir");
  return dir;
}

/**
 * Set an external working directory for a session.
 *
 * When the agent provides a workdir, we use it but don't manage its lifecycle.
 * If the session previously had a managed workdir, it's cleaned up first.
 *
 * @param session - The session to configure
 * @param workdir - The external working directory path
 */
export async function setWorkdir(session: SessionState, workdir: string): Promise<void> {
  // Clean up any existing managed workdir
  if (session.workdir && session.workdir !== workdir && session.workdirManaged) {
    await clearWorkdir(session);
  }

  session.workdir = workdir;
  session.workdirManaged = false;

  logger.debug({ session_id: session.id, workdir }, "Set external workdir");
}

/**
 * Clean up a session's working directory.
 *
 * Only removes directories that we created (workdirManaged=true).
 * External directories provided by the agent are left intact.
 *
 * @param session - The session to clean up
 */
export async function clearWorkdir(session: SessionState): Promise<void> {
  if (!session.workdir) {
    return;
  }

  // Don't delete directories we didn't create
  if (!session.workdirManaged) {
    session.workdir = undefined;
    return;
  }

  const dir = session.workdir;
  session.workdir = undefined;
  session.workdirManaged = undefined;

  try {
    await rm(dir, { recursive: true, force: true });
    logger.debug({ session_id: session.id, workdir: dir }, "Cleaned up temp workdir");
  } catch (err) {
    // Best-effort cleanup; log but don't fail
    logger.warn({ session_id: session.id, workdir: dir, error: err }, "Failed to clean workdir");
  }
}
