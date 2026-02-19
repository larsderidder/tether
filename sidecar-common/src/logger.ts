/**
 * Pino logger factory.
 *
 * Each sidecar passes its own name so log lines are distinguishable
 * when both run in the same terminal session.
 *
 * @module logger
 */

import pino from "pino";

export function createLogger(name: string, level: string, pretty: boolean): pino.Logger {
  return pino(
    {
      level,
      name,
      base: undefined,
      timestamp: pino.stdTimeFunctions.isoTime,
    },
    pretty
      ? pino.transport({
          target: "pino-pretty",
          options: { colorize: true, translateTime: "SYS:standard", ignore: "pid,hostname" },
        })
      : undefined,
  );
}
