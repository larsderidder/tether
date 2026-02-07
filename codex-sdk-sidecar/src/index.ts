/**
 * Codex SDK Sidecar - Main entry point.
 *
 * The sidecar is a lightweight HTTP service that bridges the Tether agent
 * (Python) with the Codex SDK (TypeScript). It provides:
 *
 * - REST endpoints for session control (start, input, stop)
 * - SSE streaming for real-time event delivery
 * - Working directory management
 * - Token and auth middleware
 *
 * Architecture:
 * ```
 * Agent (Python) <--HTTP/SSE--> Sidecar <--SDK--> Codex CLI
 * ```
 *
 * The agent owns session persistence and lifecycle. The sidecar only
 * maintains in-memory runtime state for active sessions.
 *
 * @module index
 */

import "dotenv/config";
import express, { Request, Response, NextFunction } from "express";
import path from "node:path";
import { mkdir, writeFile, unlink } from "node:fs/promises";
import { access } from "node:fs/promises";
import { settings } from "./settings.js";
import { logger } from "./logger.js";
import { router } from "./routes.js";

// =============================================================================
// Express App Setup
// =============================================================================

const app = express();

// Parse JSON request bodies
app.use(express.json());

// =============================================================================
// Authentication Middleware
// =============================================================================

const SIDECAR_TOKEN = settings.token();
let warnedMissingToken = false;

/**
 * Token authentication middleware.
 *
 * If TETHER_CODEX_SIDECAR_TOKEN is set, all requests must include
 * a matching X-Sidecar-Token header. If not set, auth is disabled
 * (with a warning logged once).
 */
app.use((req: Request, res: Response, next: NextFunction) => {
  // No token configured = auth disabled
  if (!SIDECAR_TOKEN) {
    if (!warnedMissingToken) {
      warnedMissingToken = true;
      logger.warn("TETHER_CODEX_SIDECAR_TOKEN not set; auth disabled");
    }
    return next();
  }

  // Validate token
  const token = req.header("x-sidecar-token");
  if (token !== SIDECAR_TOKEN) {
    return res.status(401).json({ error: "unauthorized" });
  }

  return next();
});

// =============================================================================
// Routes
// =============================================================================

// Health check endpoint
app.get("/health", (_req: Request, res: Response) => {
  res.json({ status: "ok" });
});

// Mount the API routes
app.use(router);

// =============================================================================
// Server Startup
// =============================================================================

const port = settings.port();
const host = settings.host();

async function validateCodexHomeWritable(): Promise<void> {
  const codexHome = (process.env.CODEX_HOME || "").trim();
  if (!codexHome) {
    return;
  }

  try {
    await mkdir(codexHome, { recursive: true });
    const probe = path.join(codexHome, ".tether_write_probe");
    await writeFile(probe, "ok");
    await unlink(probe);
  } catch (err) {
    logger.fatal(
      {
        codex_home: codexHome,
        error: err instanceof Error ? err.message : String(err),
      },
      "CODEX_HOME is not writable; Codex CLI cannot create sessions/logs. Fix directory permissions/ownership.",
    );
    process.exit(1);
  }
}

async function warnIfNoAuthConfigured(): Promise<void> {
  const hasApiKey =
    !!(process.env.OPENAI_API_KEY || "").trim() || !!(process.env.CODEX_API_KEY || "").trim();
  if (hasApiKey) {
    return;
  }

  const codexHome = (process.env.CODEX_HOME || "").trim();
  if (!codexHome) {
    logger.warn(
      "No OPENAI_API_KEY/CODEX_API_KEY and CODEX_HOME is unset; Codex CLI OAuth likely not configured",
    );
    return;
  }

  try {
    await access(path.join(codexHome, "auth.json"));
    return;
  } catch {
    logger.warn(
      {
        codex_home: codexHome,
        expected_auth_json: path.join(codexHome, "auth.json"),
      },
      "No OPENAI_API_KEY/CODEX_API_KEY and no auth.json found; Codex CLI will likely fail until OAuth credentials are configured",
    );
  }
}

void (async () => {
  await validateCodexHomeWritable();
  await warnIfNoAuthConfigured();
  app.listen(port, host, () => {
  logger.info({ url: `http://${host}:${port}` }, "Codex SDK Sidecar listening");
  });
})();
