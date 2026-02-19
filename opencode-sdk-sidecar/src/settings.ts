/**
 * Environment configuration for the OpenCode SDK Sidecar.
 *
 * All variables use the TETHER_OPENCODE_SIDECAR_ prefix.
 *
 * @module settings
 */

function get(name: string, defaultValue: string = ""): string {
  return process.env[name]?.trim() || defaultValue;
}

function getBool(name: string, defaultValue: boolean = false): boolean {
  const v = process.env[name]?.trim().toLowerCase();
  if (!v) return defaultValue;
  return v === "1" || v === "true" || v === "yes";
}

function getInt(name: string, defaultValue: number = 0): number {
  const v = process.env[name]?.trim();
  if (!v) return defaultValue;
  const n = parseInt(v, 10);
  return isNaN(n) ? defaultValue : n;
}

export const settings = {
  host: () => get("TETHER_OPENCODE_SIDECAR_HOST", "127.0.0.1"),
  port: () => getInt("TETHER_OPENCODE_SIDECAR_PORT", 8790),
  token: () => get("TETHER_OPENCODE_SIDECAR_TOKEN"),

  logLevel: () => get("TETHER_OPENCODE_SIDECAR_LOG_LEVEL", "info"),
  logPretty: () => getBool("TETHER_OPENCODE_SIDECAR_LOG_PRETTY"),

  turnTimeoutSeconds: () => getInt("TETHER_OPENCODE_SIDECAR_TURN_TIMEOUT_SECONDS", 0),

  /** Path to the opencode binary (default: auto-detect). */
  opencodeBin: (): string | undefined => get("OPENCODE_BIN") || undefined,
};
