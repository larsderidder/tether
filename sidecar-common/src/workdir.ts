/**
 * Working directory management for sessions.
 *
 * @module workdir
 */

import { tmpdir } from "node:os";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import type { SessionState } from "./types.js";
import type pino from "pino";

export async function ensureWorkdir(
  session: SessionState<any>,
  logger?: pino.Logger,
): Promise<string> {
  if (session.workdir) return session.workdir;

  const dir = await mkdtemp(join(tmpdir(), `tether_${session.id}_`));
  session.workdir = dir;
  session.workdirManaged = true;
  logger?.debug({ session_id: session.id, workdir: dir }, "Created temp workdir");
  return dir;
}

export async function setWorkdir(
  session: SessionState<any>,
  workdir: string,
  logger?: pino.Logger,
): Promise<void> {
  if (session.workdir && session.workdir !== workdir && session.workdirManaged) {
    await clearWorkdir(session, logger);
  }
  session.workdir = workdir;
  session.workdirManaged = false;
  logger?.debug({ session_id: session.id, workdir }, "Set external workdir");
}

export async function clearWorkdir(
  session: SessionState<any>,
  logger?: pino.Logger,
): Promise<void> {
  if (!session.workdir) return;
  if (!session.workdirManaged) {
    session.workdir = undefined;
    return;
  }
  const dir = session.workdir;
  session.workdir = undefined;
  session.workdirManaged = undefined;
  try {
    await rm(dir, { recursive: true, force: true });
    logger?.debug({ session_id: session.id, workdir: dir }, "Cleaned up temp workdir");
  } catch (err) {
    logger?.warn({ session_id: session.id, workdir: dir, error: err }, "Failed to clean workdir");
  }
}
