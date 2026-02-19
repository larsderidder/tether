import { createRouter } from "@tether/sidecar-common/routes";
import { getSession } from "./session.js";
import { runTurn } from "./opencode.js";
import { logger } from "./logger.js";

export const router = createRouter({ getSession, runTurn, logger });
