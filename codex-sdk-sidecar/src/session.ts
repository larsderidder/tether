/**
 * Session registry for the codex sidecar.
 */
import { createSessionRegistry } from "@tether/sidecar-common/session";
import type { CodexThread } from "./types.js";

export * from "@tether/sidecar-common/session";

const registry = createSessionRegistry<CodexThread>();
export const { getSession, deleteSession } = registry;
