/**
 * Session registry for the opencode sidecar.
 *
 * The thread handle here is the opencode client returned by createOpencodeServer.
 */
import { createSessionRegistry } from "@tether/sidecar-common/session";
import type { OpencodeClient } from "@opencode-ai/sdk";

export * from "@tether/sidecar-common/session";

/** Per-session opencode server state. */
export type OpencodeServerHandle = {
  client: OpencodeClient;
  close: () => void;
  url: string;
  /** Working directory this server instance is scoped to. */
  directory: string;
};

const registry = createSessionRegistry<OpencodeServerHandle>();
export const { getSession, deleteSession } = registry;
