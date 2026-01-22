export type SessionState = "CREATED" | "RUNNING" | "STOPPING" | "STOPPED" | "ERROR";

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
  directory: string | null;
  directory_has_git: boolean;
};

export type EventEnvelope = {
  session_id: string;
  ts: string;
  seq: number;
  type: "session_state" | "output" | "error" | "metadata" | "header" | "heartbeat";
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

const BASE_KEY = "tether_base_url";
const TOKEN_KEY = "tether_token";
const LEGACY_BASE_KEY_V1 = "codex_base_url";
const LEGACY_TOKEN_KEY_V1 = "codex_token";
export const AUTH_REQUIRED_EVENT = "tether:auth-required";

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
  const data = await fetchJson<{ sessions: Session[] }>("/api/sessions");
  return data.sessions;
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
  const data = await fetchJson<{ session: Session }>("/api/sessions", {
    method: "POST",
    body: JSON.stringify(payload)
  });
  return data.session;
}

export async function getSession(id: string): Promise<Session> {
  const data = await fetchJson<{ session: Session }>(`/api/sessions/${id}`);
  return data.session;
}

export async function startSession(id: string, prompt: string): Promise<Session> {
  const data = await fetchJson<{ session: Session }>(`/api/sessions/${id}/start`, {
    method: "POST",
    body: JSON.stringify({ prompt })
  });
  return data.session;
}

export async function stopSession(id: string): Promise<Session> {
  const data = await fetchJson<{ session: Session }>(`/api/sessions/${id}/stop`, {
    method: "POST"
  });
  return data.session;
}

export function stopSessionKeepalive(id: string): void {
  const token = getToken();
  const headers: HeadersInit = {};
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  try {
    fetch(buildUrl(`/api/sessions/${id}/stop`), {
      method: "POST",
      headers,
      keepalive: true
    }).catch(() => undefined);
  } catch {
    // best-effort on unload
  }
}

export async function sendInput(id: string, text: string): Promise<Session> {
  const data = await fetchJson<{ session: Session }>(`/api/sessions/${id}/input`, {
    method: "POST",
    body: JSON.stringify({ text })
  });
  return data.session;
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
  const data = await fetchJson<{ session: Session }>(`/api/sessions/${id}/rename`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
  return data.session;
}

export async function openEventStream(
  id: string,
  onEvent: (event: EventEnvelope) => void,
  onError: (error: unknown) => void
): Promise<() => void> {
  const token = getToken();
  const headers: HeadersInit = {};
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  const res = await fetch(buildUrl(`/api/events/sessions/${id}`), { headers });
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
