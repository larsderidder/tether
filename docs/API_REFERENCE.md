# API Reference

Complete REST API and SSE specification. All agents should read this.

## Base URL

`http://localhost:8787` (configurable via `TETHER_AGENT_PORT`)

## Authentication

Bearer token via `Authorization: Bearer <token>` when `TETHER_AGENT_TOKEN` is set.
If unset, auth is disabled. Enforced by `agent/tether/api/deps.py:require_token()`.

---

## Session Endpoints (`/api/sessions`)

### `GET /api/sessions`
List all sessions. Returns `Session[]`.

### `POST /api/sessions`
Create session. Supports both local and external agent sessions.

Request:
```json
{
  "repo_id": "path",
  "base_ref": "main",
  "directory": "/path/to/project",
  "adapter": "claude_local",
  "agent_name": "Claude Code",
  "platform": "telegram"
}
```

### `GET /api/sessions/{id}`
Get single session.

### `DELETE /api/sessions/{id}`
Delete session. Fails if RUNNING/INTERRUPTING (409).

### `POST /api/sessions/{id}/start`
Start session with prompt.
```json
{"prompt": "Implement feature X", "approval_choice": 1}
```

### `POST /api/sessions/{id}/input`
Send user input to AWAITING_INPUT session.
```json
{"text": "Yes, proceed"}
```

### `POST /api/sessions/{id}/interrupt`
Interrupt running session. Transitions RUNNING -> INTERRUPTING -> AWAITING_INPUT.

### `PATCH /api/sessions/{id}/rename`
```json
{"name": "New name"}
```

### `PATCH /api/sessions/{id}/approval-mode`
```json
{"approval_mode": 1}
```

### `POST /api/sessions/{id}/permission`
Respond to a permission request.
```json
{"request_id": "perm_1", "allow": true, "message": "Approved"}
```

### `GET /api/sessions/{id}/usage`
Returns aggregated token usage from metadata events.
```json
{"input_tokens": 1000, "output_tokens": 500, "total_cost_usd": 0.012}
```

### `GET /api/sessions/{id}/diff`
Returns git diff for the session's working directory.

---

## External Agent Endpoints

### `POST /api/sessions/{id}/events`
Push events from external agents (output, status, permission_request).
```json
{"type": "output", "data": {"text": "Hello"}}
{"type": "permission_request", "data": {"request_id": "p1", "tool_name": "Read", "tool_input": {}}}
```

### `GET /api/sessions/{id}/events/poll`
Poll for events (user_input, approval_response). Params: `since_seq`.

---

## External Session Discovery (`/api/external-sessions`)

### `GET /api/external-sessions`
List discoverable Claude Code and Codex sessions on the local machine.
Params: `limit`, `runner_type`, `directory`.

### `GET /api/external-sessions/{id}/history`
Get message history for an external session.
Params: `runner_type`, `limit`.

### `POST /api/sessions/attach`
Attach a discovered external session to Tether.
```json
{"external_id": "...", "runner_type": "claude_code", "directory": "/path"}
```

### `POST /api/sessions/{id}/sync`
Sync new messages from an attached external session.

---

## Directory Endpoints (`/api/directories`)

### `GET /api/directories/check`
Validate directory path. Params: `path`. Returns exists, is_directory, has_git.

### `GET /api/directories/diff`
Get git diff for a directory. Params: `path`.

---

## SSE Streaming (`/api/events`)

### `GET /api/events/sessions/{id}`
Server-Sent Events stream. Params: `since_seq`, `limit`.

Event envelope:
```json
{"session_id": "...", "ts": "...", "seq": 12, "type": "output", "data": {...}}
```

Event types:
- `session_state` - `{state: "RUNNING"}`
- `header` - `{title, model, provider, sandbox, approval}`
- `output` - `{stream, text, kind, final, is_history?}`
- `output_final` - Accumulated final output blob
- `metadata` - `{key, value, raw}`
- `heartbeat` - `{elapsed_s, done}`
- `input_required` - `{session_name, last_output, truncated}`
- `user_input` - `{text, is_history?}`
- `error` - `{code, message}`
- `warning` - `{code, message}`
- `permission_request` - `{request_id, tool_name, tool_input, suggestions}`
- `permission_resolved` - `{request_id, resolved_by, allowed, message}`

---

## Other Endpoints

### `GET /api/health`
Returns `{"ok": true}`.

### `POST /api/debug/clear_data`
Clear all persisted data (debug only).

---

## Key Files

- `agent/tether/api/sessions.py` - Session CRUD + lifecycle
- `agent/tether/api/external_sessions.py` - External session discovery + attach
- `agent/tether/api/events.py` - SSE stream endpoint
- `agent/tether/api/emit.py` - Event emission helpers
- `agent/tether/api/schemas.py` - Request/response Pydantic models
- `agent/tether/api/deps.py` - Auth dependency
- `agent/tether/api/state.py` - State machine + session locking
- `agent/tether/sse.py` - SSE stream generator
