/**
 * Codex-sidecar types.
 *
 * SessionState is parameterised with the Codex thread handle.
 */
import type { SessionState as BaseSessionState } from "@tether/sidecar-common/types";
import type { Codex } from "../../codex-src/sdk/typescript/src/index.js";

export type CodexThread = ReturnType<Codex["startThread"]>;
export type SessionState = BaseSessionState<CodexThread>;
