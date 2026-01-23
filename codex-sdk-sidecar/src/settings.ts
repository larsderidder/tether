/**
 * Centralized environment configuration for the Codex SDK Sidecar.
 *
 * All environment variables are accessed through this module to ensure
 * consistent handling of defaults and type conversion. Variables use
 * the TETHER_CODEX_SIDECAR_ prefix for namespacing.
 *
 * @module settings
 */

// =============================================================================
// Helper Functions
// =============================================================================

/**
 * Get a string environment variable.
 *
 * @param name - Environment variable name
 * @param defaultValue - Value to return if not set
 * @returns The trimmed value or default
 */
function get(name: string, defaultValue: string = ""): string {
  return process.env[name]?.trim() || defaultValue;
}

/**
 * Get a boolean environment variable.
 *
 * Recognizes "1", "true", and "yes" (case-insensitive) as true.
 *
 * @param name - Environment variable name
 * @param defaultValue - Value to return if not set
 * @returns Parsed boolean or default
 */
function getBool(name: string, defaultValue: boolean = false): boolean {
  const value = process.env[name]?.trim().toLowerCase();
  if (!value) return defaultValue;
  return value === "1" || value === "true" || value === "yes";
}

/**
 * Get an integer environment variable.
 *
 * @param name - Environment variable name
 * @param defaultValue - Value to return if not set or invalid
 * @returns Parsed integer or default
 */
function getInt(name: string, defaultValue: number = 0): number {
  const value = process.env[name]?.trim();
  if (!value) return defaultValue;
  const parsed = parseInt(value, 10);
  return isNaN(parsed) ? defaultValue : parsed;
}

// =============================================================================
// Settings Object
// =============================================================================

/**
 * Centralized settings for the Codex SDK Sidecar.
 *
 * All settings are accessed as functions to allow for dynamic
 * environment variable reads (useful for testing).
 *
 * @example
 * ```typescript
 * import { settings } from "./settings.js";
 *
 * const port = settings.port();
 * if (settings.logPretty()) {
 *   // enable pretty logging
 * }
 * ```
 */
export const settings = {
  // ---------------------------------------------------------------------------
  // Server Settings
  // ---------------------------------------------------------------------------

  /**
   * Host address to bind the HTTP server to.
   *
   * Use "0.0.0.0" to accept connections from any interface,
   * or "127.0.0.1" for localhost only.
   *
   * @returns Host address (default: "127.0.0.1")
   * @env TETHER_CODEX_SIDECAR_HOST
   */
  host: (): string => get("TETHER_CODEX_SIDECAR_HOST", "127.0.0.1"),

  /**
   * Port number to bind the HTTP server to.
   *
   * @returns Port number (default: 8788)
   * @env TETHER_CODEX_SIDECAR_PORT
   */
  port: (): number => getInt("TETHER_CODEX_SIDECAR_PORT", 8788),

  /**
   * Authentication token for the sidecar API.
   *
   * When set, all requests must include this token in the
   * X-Sidecar-Token header. When empty, authentication is disabled.
   *
   * @returns Token string or empty if auth disabled
   * @env TETHER_CODEX_SIDECAR_TOKEN
   */
  token: (): string => get("TETHER_CODEX_SIDECAR_TOKEN"),

  // ---------------------------------------------------------------------------
  // Logging Settings
  // ---------------------------------------------------------------------------

  /**
   * Minimum log level to output.
   *
   * Levels in order: trace, debug, info, warn, error, fatal
   *
   * @returns Log level string (default: "info")
   * @env TETHER_CODEX_SIDECAR_LOG_LEVEL
   */
  logLevel: (): string => get("TETHER_CODEX_SIDECAR_LOG_LEVEL", "info"),

  /**
   * Enable pretty-printed, colorized log output.
   *
   * Useful for development. Disable in production for JSON logs.
   *
   * @returns True if pretty logging enabled (default: false)
   * @env TETHER_CODEX_SIDECAR_LOG_PRETTY
   */
  logPretty: (): boolean => getBool("TETHER_CODEX_SIDECAR_LOG_PRETTY"),

  // ---------------------------------------------------------------------------
  // Session Settings
  // ---------------------------------------------------------------------------

  /**
   * Maximum duration in seconds for a single turn.
   *
   * If a turn exceeds this duration, it's aborted with a TIMEOUT error.
   * Set to 0 to disable timeout (turns can run indefinitely).
   *
   * @returns Timeout in seconds, or 0 for no timeout (default: 0)
   * @env TETHER_CODEX_SIDECAR_TURN_TIMEOUT_SECONDS
   */
  turnTimeoutSeconds: (): number => getInt("TETHER_CODEX_SIDECAR_TURN_TIMEOUT_SECONDS", 0),

  // ---------------------------------------------------------------------------
  // Codex SDK Settings
  // ---------------------------------------------------------------------------

  /**
   * Path to the Codex CLI binary.
   *
   * If not set, the SDK will attempt to find codex in PATH.
   *
   * @returns Path to codex binary, or undefined to use default
   * @env TETHER_CODEX_SIDECAR_CODEX_BIN
   */
  codexBin: (): string | undefined => get("TETHER_CODEX_SIDECAR_CODEX_BIN") || undefined,

  /**
   * Model to use for Codex threads.
   *
   * Passed to the Codex SDK's ThreadOptions.model.
   *
   * @returns Model identifier, or undefined to use SDK default
   * @env TETHER_CODEX_SIDECAR_MODEL
   */
  codexModel: (): string | undefined => get("TETHER_CODEX_SIDECAR_MODEL") || undefined,

  /**
   * Sandbox mode for Codex execution.
   *
   * Controls file system access restrictions:
   * - "workspace-write": Can write within working directory
   * - "workspace-read-only": Read-only access to working directory
   * - "none": No sandbox restrictions
   *
   * @returns Sandbox mode, or undefined to use SDK default
   * @env TETHER_CODEX_SIDECAR_SANDBOX_MODE
   */
  codexSandboxMode: (): string | undefined => get("TETHER_CODEX_SIDECAR_SANDBOX_MODE") || undefined,

  /**
   * Approval policy for Codex actions.
   *
   * Controls whether dangerous actions require user approval:
   * - "auto-approve": All actions proceed automatically
   * - "on-request": Prompt for approval when needed
   *
   * @returns Approval policy, or undefined to use SDK default
   * @env TETHER_CODEX_SIDECAR_APPROVAL_POLICY
   */
  codexApprovalPolicy: (): string | undefined =>
    get("TETHER_CODEX_SIDECAR_APPROVAL_POLICY") || undefined,
};
