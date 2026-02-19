/**
 * Codex SDK Sidecar — entry point.
 *
 * @module index
 */

import "dotenv/config";
import path from "node:path";
import { mkdir, writeFile, unlink, access } from "node:fs/promises";
import { createSidecarApp } from "@tether/sidecar-common/server";
import { settings } from "./settings.js";
import { logger } from "./logger.js";
import { getSession } from "./session.js";
import { runTurn } from "./codex.js";

const app = createSidecarApp({
  name: "codex-sdk-sidecar",
  logger,
  token: settings.token(),
  getSession,
  runTurn,
});

// ---------------------------------------------------------------------------
// Startup checks
// ---------------------------------------------------------------------------

async function validateCodexHomeWritable(): Promise<void> {
  const codexHome = (process.env.CODEX_HOME || "").trim();
  if (!codexHome) return;
  try {
    await mkdir(codexHome, { recursive: true });
    const probe = path.join(codexHome, ".tether_write_probe");
    await writeFile(probe, "ok");
    await unlink(probe);
  } catch (err) {
    logger.fatal(
      { codex_home: codexHome, error: err instanceof Error ? err.message : String(err) },
      "CODEX_HOME is not writable; Codex CLI cannot create sessions/logs.",
    );
    process.exit(1);
  }
}

async function warnIfNoAuthConfigured(): Promise<void> {
  const hasApiKey = !!(process.env.OPENAI_API_KEY || process.env.CODEX_API_KEY || "").trim();
  if (hasApiKey) return;
  const codexHome = (process.env.CODEX_HOME || "").trim();
  if (!codexHome) {
    logger.warn("No OPENAI_API_KEY/CODEX_API_KEY and CODEX_HOME unset; OAuth may not be configured");
    return;
  }
  try {
    await access(path.join(codexHome, "auth.json"));
  } catch {
    logger.warn(
      { codex_home: codexHome },
      "No API key and no auth.json; Codex CLI will likely fail",
    );
  }
}

void (async () => {
  await validateCodexHomeWritable();
  await warnIfNoAuthConfigured();
  const port = settings.port();
  const host = settings.host();
  app.listen(port, host, () => {
    logger.info({ url: `http://${host}:${port}` }, "Codex SDK Sidecar listening");
  });
})();
