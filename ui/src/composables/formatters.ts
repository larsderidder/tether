import { formatDistanceToNow } from "date-fns";

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
