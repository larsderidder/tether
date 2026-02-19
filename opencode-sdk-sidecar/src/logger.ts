import { createLogger } from "@tether/sidecar-common/logger";
import { settings } from "./settings.js";

export const logger = createLogger(
  "opencode-sdk-sidecar",
  settings.logLevel(),
  settings.logPretty(),
);
