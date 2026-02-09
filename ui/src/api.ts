export type SessionState = "CREATED" | "RUNNING" | "AWAITING_INPUT" | "INTERRUPTING" | "ERROR";

export type Session = {
  id: string;
  state: SessionState;
  name: string | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  last_activity_at: string;
  exit_code: number | null;
  summary: string | null;
  runner_header: string | null;
  runner_type: string | null;
  runner_session_id: string | null;
  directory: string | null;
  directory_has_git: boolean;
  message_count: number;
  has_pending_permission: boolean;
  approval_mode: ApprovalMode | null;  // null = use global default
};

export type EventEnvelope = {
  session_id: string;
  ts: string;
  seq: number;
  type: "session_state" | "output" | "error" | "metadata" | "header" | "heartbeat" | "user_input" | "input_required" | "permission_request" | "permission_resolved";
  data?: unknown;
}

export type PermissionRequestData = {
  request_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  suggestions?: unknown[];
};

export type PermissionResolvedData = {
  request_id: string;
  resolved_by: "timeout" | "cancelled" | "user";
  allowed: boolean;
  message?: string;
};

export type HeaderData = {
  title: string;
  model?: string;
  provider?: string;
  sandbox?: string;
  approval?: string;
  session_id?: string;
  thread_id?: string;
  data: Record<string, unknown>;
};

export type DiffFile = {
  path: string;
  hunks: number;
  patch: string;
};

export type DiffResponse = {
  diff: string;
  files?: DiffFile[];
};

export type DirectoryCheck = {
  path: string;
  exists: boolean;
  is_git: boolean;
};

export type ExternalRunnerType = "claude_code" | "codex";

export type ExternalSessionSummary = {
  id: string;
  runner_type: ExternalRunnerType;
  directory: string;
  first_prompt: string | null;
  last_activity: string;
  message_count: number;
  is_running: boolean;
};

export type ExternalSessionMessage = {
  role: "user" | "assistant";
  content: string;
  timestamp: string | null;
};

export type ExternalSessionDetail = ExternalSessionSummary & {
  messages: ExternalSessionMessage[];
};

export type BridgeStatusInfo = {
  platform: string;
  status: "running" | "error" | "not_configured";
  initialized_at: string | null;
  error_message: string | null;
};

export type SessionActivityInfo = {
  session_id: string;
  name: string;
  state: string;
  platform: string | null;
  last_activity_at: string;
  message_count: number;
};

export type SessionStats = {
  total: number;
  by_state: Record<string, number>;
  by_platform: Record<string, number>;
  recent_activity: SessionActivityInfo[];
};

const BASE_KEY = "tether_base_url";
const TOKEN_KEY = "tether_token";
const APPROVAL_MODE_KEY = "tether_approval_mode";
const LEGACY_BASE_KEY_V1 = "codex_base_url";
const LEGACY_TOKEN_KEY_V1 = "codex_token";
export const AUTH_REQUIRED_EVENT = "tether:auth-required";

export type ApprovalMode = 0 | 1 | 2;
// 0 = Interactive (ask for permissions)
// 1 = Auto-approve edits only
// 2 = Full auto-approve (bypass all)

export function getBaseUrl(): string {
  return localStorage.getItem(BASE_KEY) || localStorage.getItem(LEGACY_BASE_KEY_V1) || "";
}

export function setBaseUrl(value: string): void {
  localStorage.setItem(BASE_KEY, value);
}

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || localStorage.getItem(LEGACY_TOKEN_KEY_V1) || "";
}

export function setToken(value: string): void {
  localStorage.setItem(TOKEN_KEY, value);
}

export function getApprovalMode(): ApprovalMode {
  const stored = localStorage.getItem(APPROVAL_MODE_KEY);
  if (stored === "0") return 0;
  if (stored === "1") return 1;
  return 2; // Default to full auto-approve
}

export function setApprovalMode(value: ApprovalMode): void {
  localStorage.setItem(APPROVAL_MODE_KEY, String(value));
}

function buildUrl(path: string): string {
  const base = getBaseUrl();
  if (!base) {
    return path;
  }
  return `${base.replace(/\/$/, "")}${path}`;
}

function notifyAuthRequired(): void {
  if (typeof window === "undefined") {
    return;
  }
  window.dispatchEvent(new CustomEvent(AUTH_REQUIRED_EVENT));
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const headers: HeadersInit = {
    "Content-Type": "application/json",
    ...(init?.headers || {})
  };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  const res = await fetch(buildUrl(path), { ...init, headers });
  if (!res.ok) {
    if (res.status === 401) {
      notifyAuthRequired();
    }
    throw new Error(`Request failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export async function listSessions(): Promise<Session[]> {
  return await fetchJson<Session[]>("/api/sessions");
}

export type CreateSessionOptions = {
  repoId?: string;
  directory?: string;
};

export async function createSession(options: CreateSessionOptions = {}): Promise<Session> {
  const payload: Record<string, string> = {};
  if (options.repoId) {
    payload.repo_id = options.repoId;
  }
  if (options.directory) {
    payload.directory = options.directory;
  }
  return await fetchJson<Session>("/api/sessions", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export async function getSession(id: string): Promise<Session> {
  return await fetchJson<Session>(`/api/sessions/${id}`);
}

export async function startSession(id: string, prompt: string, approvalChoice?: ApprovalMode): Promise<Session> {
  const approval_choice = approvalChoice ?? getApprovalMode();
  return await fetchJson<Session>(`/api/sessions/${id}/start`, {
    method: "POST",
    body: JSON.stringify({ prompt, approval_choice })
  });
}

export async function interruptSession(id: string): Promise<Session> {
  return await fetchJson<Session>(`/api/sessions/${id}/interrupt`, {
    method: "POST"
  });
}

export function interruptSessionKeepalive(id: string): void {
  const token = getToken();
  const headers: HeadersInit = {};
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  try {
    fetch(buildUrl(`/api/sessions/${id}/interrupt`), {
      method: "POST",
      headers,
      keepalive: true
    }).catch(() => undefined);
  } catch {
    // best-effort on unload
  }
}

export async function sendInput(id: string, text: string): Promise<Session> {
  return await fetchJson<Session>(`/api/sessions/${id}/input`, {
    method: "POST",
    body: JSON.stringify({ text })
  });
}

export async function getDiff(id: string): Promise<DiffResponse> {
  const data = await fetchJson<DiffResponse>(`/api/sessions/${id}/diff`);
  return data;
}

export async function getDirectoryDiff(path: string): Promise<DiffResponse> {
  const params = new URLSearchParams({ path });
  const data = await fetchJson<DiffResponse>(`/api/directories/diff?${params.toString()}`);
  return data;
}

export async function checkDirectory(path: string): Promise<DirectoryCheck> {
  const params = new URLSearchParams({ path });
  const data = await fetchJson<DirectoryCheck>(`/api/directories/check?${params.toString()}`);
  return data;
}

export async function deleteSession(id: string): Promise<void> {
  await fetchJson<{ ok: boolean }>(`/api/sessions/${id}`, {
    method: "DELETE"
  });
}

export async function clearAllData(): Promise<void> {
  await fetchJson<{ ok: boolean }>(`/api/debug/clear_data`, {
    method: "POST"
  });
}

export async function renameSession(id: string, name: string): Promise<Session> {
  return await fetchJson<Session>(`/api/sessions/${id}/rename`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}

export async function updateSessionApprovalMode(
  id: string,
  approvalMode: ApprovalMode | null
): Promise<Session> {
  return await fetchJson<Session>(`/api/sessions/${id}/approval-mode`, {
    method: "PATCH",
    body: JSON.stringify({ approval_mode: approvalMode }),
  });
}

export type ListExternalSessionsOptions = {
  directory?: string;
  runner_type?: ExternalRunnerType;
  limit?: number;
};

export async function listExternalSessions(
  options: ListExternalSessionsOptions = {}
): Promise<ExternalSessionSummary[]> {
  const params = new URLSearchParams();
  if (options.directory) {
    params.set("directory", options.directory);
  }
  if (options.runner_type) {
    params.set("runner_type", options.runner_type);
  }
  if (options.limit) {
    params.set("limit", String(options.limit));
  }
  const query = params.toString();
  return await fetchJson<ExternalSessionSummary[]>(
    `/api/external-sessions${query ? `?${query}` : ""}`
  );
}

export async function getExternalSessionHistory(
  id: string,
  runnerType: ExternalRunnerType,
  limit?: number
): Promise<ExternalSessionDetail> {
  const params = new URLSearchParams({ runner_type: runnerType });
  if (limit) {
    params.set("limit", String(limit));
  }
  return await fetchJson<ExternalSessionDetail>(
    `/api/external-sessions/${id}/history?${params.toString()}`
  );
}

export async function attachToExternalSession(
  externalId: string,
  runnerType: ExternalRunnerType,
  directory: string
): Promise<Session> {
  return await fetchJson<Session>("/api/sessions/attach", {
    method: "POST",
    body: JSON.stringify({
      external_id: externalId,
      runner_type: runnerType,
      directory,
    }),
  });
}

export type SyncResult = {
  synced: number;
  total: number;
};

export async function syncSession(id: string): Promise<SyncResult> {
  const data = await fetchJson<SyncResult>(`/api/sessions/${id}/sync`, {
    method: "POST",
  });
  return data;
}

export type PermissionResponse = {
  request_id: string;
  allow: boolean;
  message?: string;
  updated_input?: Record<string, unknown>;
};

export async function respondToPermission(
  sessionId: string,
  response: PermissionResponse
): Promise<void> {
  await fetchJson<{ ok: boolean }>(`/api/sessions/${sessionId}/permission`, {
    method: "POST",
    body: JSON.stringify(response),
  });
}

export async function getBridgeStatus(): Promise<{ bridges: BridgeStatusInfo[] }> {
  return await fetchJson<{ bridges: BridgeStatusInfo[] }>("/api/status/bridges");
}

export async function getSessionStats(): Promise<SessionStats> {
  return await fetchJson<SessionStats>("/api/status/sessions");
}

export async function openEventStream(
  id: string,
  onEvent: (event: EventEnvelope) => void,
  onError: (error: unknown) => void,
  options?: { since?: number }
): Promise<() => void> {
  const token = getToken();
  const headers: HeadersInit = {};
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  const params = new URLSearchParams();
  if (options?.since && options.since > 0) {
    params.set("since", String(options.since));
  }
  const query = params.toString();
  const url = `/api/events/sessions/${id}${query ? `?${query}` : ""}`;
  const res = await fetch(buildUrl(url), { headers });
  if (!res.ok || !res.body) {
    if (res.status === 401) {
      notifyAuthRequired();
    }
    throw new Error(`Stream error: ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let cancelled = false;

  const pump = async (): Promise<void> => {
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          // Stream ended - trigger reconnect if not explicitly cancelled
          if (!cancelled) {
            onError(new Error("Stream closed unexpectedly"));
          }
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";
        for (const part of parts) {
          const lines = part.split("\n");
          const dataLines = lines
            .filter((line) => line.startsWith("data: "))
            .map((line) => line.slice(6));
          if (!dataLines.length) {
            continue;
          }
          try {
            const payload = JSON.parse(dataLines.join(""));
            onEvent(payload as EventEnvelope);
          } catch (err) {
            onError(err);
          }
        }
      }
    } catch (err) {
      if (!cancelled) {
        onError(err);
      }
    }
  };

  pump();

  return () => {
    cancelled = true;
    reader.cancel();
  };
}
