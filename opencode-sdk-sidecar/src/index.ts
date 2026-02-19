/**
 * OpenCode SDK Sidecar — entry point.
 *
 * @module index
 */

import "dotenv/config";
import { createSidecarApp } from "@tether/sidecar-common/server";
import { settings } from "./settings.js";
import { logger } from "./logger.js";
import { getSession } from "./session.js";
import { runTurn } from "./opencode.js";

const app = createSidecarApp({
  name: "opencode-sdk-sidecar",
  logger,
  token: settings.token(),
  getSession,
  runTurn,
});

const port = settings.port();
const host = settings.host();

app.listen(port, host, () => {
  logger.info({ url: `http://${host}:${port}` }, "OpenCode SDK Sidecar listening");
});
