import pino from "pino";

const level = process.env.CODEX_SDK_SIDECAR_LOG_LEVEL || process.env.SIDECAR_LOG_LEVEL || "info";
const pretty =
  process.env.CODEX_SDK_SIDECAR_LOG_PRETTY === "1" ||
  process.env.SIDECAR_LOG_PRETTY === "1";

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
