import pino from "pino";
import { settings } from "./settings.js";

const level = settings.logLevel();
const pretty = settings.logPretty();

export const logger = pino(
  {
    level,
    name: "codex-sdk-sidecar",
    base: undefined,
    timestamp: pino.stdTimeFunctions.isoTime,
  },
  pretty
    ? pino.transport({
        target: "pino-pretty",
        options: {
          colorize: true,
          translateTime: "SYS:standard",
          ignore: "pid,hostname",
        },
      })
    : undefined,
);
