import { createLogger } from "@tether/sidecar-common/logger";
import { settings } from "./settings.js";

export const logger = createLogger("codex-sdk-sidecar", settings.logLevel(), settings.logPretty());
