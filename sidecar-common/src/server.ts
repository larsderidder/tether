/**
 * Express app factory for Tether sidecars.
 *
 * Each sidecar calls createSidecarApp() with its dependencies and gets
 * back a configured Express app ready to listen.
 *
 * @module server
 */

import express, { Request, Response, NextFunction } from "express";
import type pino from "pino";
import type { RunTurn, SessionState } from "./types.js";
import { createRouter } from "./routes.js";

export type SidecarAppConfig = {
  /** Logger instance for the sidecar. */
  logger: pino.Logger;
  /** Optional auth token. When set, all requests must include X-Sidecar-Token. */
  token?: string;
  /** Session registry getSession function. */
  getSession: (id: string) => SessionState<any>;
  /** Agent-specific turn runner. */
  runTurn: RunTurn;
  /** Sidecar name for log messages (e.g. "codex-sdk-sidecar"). */
  name: string;
};

export function createSidecarApp(config: SidecarAppConfig): express.Application {
  const { logger, token, getSession, runTurn, name } = config;
  const app = express();

  app.use(express.json());

  // Auth middleware
  let warnedMissingToken = false;
  app.use((req: Request, res: Response, next: NextFunction) => {
    if (!token) {
      if (!warnedMissingToken) {
        warnedMissingToken = true;
        logger.warn(`${name}: no token set; auth disabled`);
      }
      return next();
    }
    const provided = req.header("x-sidecar-token");
    if (provided !== token) {
      return res.status(401).json({ error: "unauthorized" });
    }
    return next();
  });

  // Health check
  app.get("/health", (_req: Request, res: Response) => {
    res.json({ status: "ok", sidecar: name });
  });

  // API routes
  app.use(createRouter({ getSession, runTurn, logger }));

  return app;
}
