import { formatDistanceToNow } from "date-fns";

/** Display agent names consistently for adapters and discovered runners. */
export function formatAgentName(type: string | null | undefined): string {
  const labels: Record<string, string> = {
    pi: "Pi",
    pi_rpc: "Pi",
    claude: "Claude",
    claude_code: "Claude",
    claude_auto: "Claude",
    claude_subprocess: "Claude",
    "claude-local": "Claude",
    "claude-subprocess": "Claude",
    claude_api: "Claude API",
    codex: "Codex",
    codex_sdk_sidecar: "Codex",
    opencode: "OpenCode",
    opencode_sdk_sidecar: "OpenCode",
    automation: "Automation",
    litellm: "LiteLLM",
  };
  return labels[type || ""] || "Agent";
}

export function formatState(state: string | undefined): string {
  if (!state) return "";
  const labels: Record<string, string> = {
    CREATED: "Ready",
    RUNNING: "Running",
    AWAITING_INPUT: "Awaiting input",
    INTERRUPTING: "Interrupting",
    ERROR: "Error"
  };
  return labels[state] || state.toLowerCase().replace(/_/g, " ");
}

export function formatTime(timestamp: string): string {
  const date = new Date(timestamp);
  return formatDistanceToNow(date, { addSuffix: true });
}

export function formatSessionId(id: string): string {
  return id.slice(0, 8);
}

export function getStatusDotClass(state: string | undefined): string {
  switch (state) {
    case "RUNNING":
      return "bg-emerald-500";
    case "AWAITING_INPUT":
      return "bg-amber-400 animate-pulse";
    case "INTERRUPTING":
      return "bg-amber-500";
    case "ERROR":
      return "bg-rose-500";
    case "CREATED":
      return "bg-blue-400";
    default:
      return "";
  }
}
