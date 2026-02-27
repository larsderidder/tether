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
Create session. Supports local directory sessions and cloned-repo sessions.

**Local directory:**
```json
{
  "directory": "/path/to/project",
  "adapter": "claude_auto",
  "platform": "telegram"
}
```

**Clone from git URL:**
```json
{
  "clone_url": "git@github.com:owner/repo.git",
  "clone_branch": "main",
  "shallow": false,
  "auto_branch": true,
  "adapter": "claude_auto"
}
```

`clone_url` and `directory` are mutually exclusive.

| Field | Type | Description |
|-------|------|-------------|
| `directory` | `str?` | Local working directory |
| `clone_url` | `str?` | Git URL to clone |
| `clone_branch` | `str?` | Branch to check out at clone time (default: repo default) |
| `shallow` | `bool` | Shallow clone (`--depth 1`) |
| `auto_branch` | `bool` | Create a `tether/<id>` working branch after clone |
| `adapter` | `str?` | Runner adapter (see Runners doc) |
| `platform` | `str?` | `"telegram"`, `"slack"`, `"discord"` |

**Response** includes `clone_url`, `clone_branch`, and `working_branch` fields.

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

## Git Endpoints (`/api/sessions/{id}/git`)

All git endpoints require the session to have a directory that is a git
repository. They return `422` if the directory has no `.git` folder.
Write endpoints (`commit`, `push`, `branch`, `checkout`, `pr`) return `409`
if the session is currently `RUNNING` or `INTERRUPTING`.

### `GET /sessions/{id}/git`
Git status for the session workspace.
```json
{
  "branch": "tether/a1b2c3",
  "remote_url": "git@github.com:owner/repo.git",
  "remote_branch": "origin/tether/a1b2c3",
  "ahead": 2,
  "behind": 0,
  "dirty": true,
  "changed_files": [
    {"path": "src/main.py", "status": "modified", "staged": false}
  ],
  "staged_count": 0,
  "unstaged_count": 1,
  "untracked_count": 0,
  "last_commit": {"hash": "abc1234", "message": "Checkpoint: turn 3", "author": "Tether", "timestamp": "2026-02-27T..."}
}
```

### `GET /sessions/{id}/git/log`
Recent commits. Query param: `count` (1–100, default 10).
Returns `GitCommit[]`.

### `POST /sessions/{id}/git/commit`
Stage all changes and create a commit.
```json
{"message": "Fix the auth bug", "add_all": true}
```
Returns the new `GitCommit`.

### `POST /sessions/{id}/git/push`
Push the current branch to origin.
```json
{"remote": "origin", "branch": null}
```
Returns `{"success": true, "remote": "...", "branch": "..."}`.

### `POST /sessions/{id}/git/branch`
Create a new branch.
```json
{"name": "feature/x", "checkout": true}
```
Returns `{"branch": "feature/x"}`.

### `POST /sessions/{id}/git/checkout`
Check out an existing branch.
```json
{"branch": "main"}
```
Returns `{"branch": "main"}`.

### `POST /sessions/{id}/git/pr`
Create a pull request (GitHub) or merge request (GitLab).
Requires `gh` or `glab` CLI installed and authenticated on the server.
```json
{
  "title": "Fix auth bug",
  "body": "Closes #42",
  "base": "main",
  "draft": false,
  "auto_push": true
}
```
Returns:
```json
{"url": "https://github.com/owner/repo/pull/7", "number": 7, "forge": "github", "draft": false}
```

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
List discoverable Claude Code, Codex, OpenCode, and Pi sessions on the local machine.
Params: `limit`, `runner_type` (`claude_code`, `codex`, `opencode`, `pi`), `directory`.

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
- `checkpoint` - `{commit_hash, message, files_changed}` — emitted after auto-checkpoint commit

---

## Other Endpoints

### `GET /api/health`
Returns `{"ok": true}`.

### `POST /api/debug/clear_data`
Clear all persisted data (debug only).

---

## Auto-Checkpoint Settings

When `TETHER_GIT_AUTO_CHECKPOINT=true`, Tether commits all changes after each
agent turn in cloned workspaces that are git repos.

| Env Var | Default | Description |
|---------|---------|-------------|
| `TETHER_GIT_AUTO_CHECKPOINT` | `false` | Enable auto-checkpoint commits |
| `TETHER_GIT_AUTO_BRANCH` | `false` | Auto-create working branch on clone |
| `TETHER_GIT_BRANCH_PATTERN` | `tether/{session_id}` | Working branch name pattern |
| `TETHER_GIT_USER_NAME` | `"Tether"` | git `user.name` in cloned workspaces |
| `TETHER_GIT_USER_EMAIL` | `"tether@localhost"` | git `user.email` in cloned workspaces |

## Key Files

- `agent/tether/api/sessions.py` - Session CRUD + lifecycle (incl. clone flow)
- `agent/tether/api/git.py` - Git status, log, and action endpoints
- `agent/tether/api/external_sessions.py` - External session discovery + attach
- `agent/tether/api/events.py` - SSE stream endpoint
- `agent/tether/api/emit.py` - Event emission helpers (incl. `emit_checkpoint`)
- `agent/tether/api/schemas.py` - Request/response Pydantic models
- `agent/tether/api/deps.py` - Auth dependency
- `agent/tether/api/state.py` - State machine + session locking
- `agent/tether/git_ops.py` - Git operations (status, log, commit, push, branch, checkout, PR)
- `agent/tether/workspace.py` - Workspace lifecycle (clone, cleanup, identity config)
- `agent/tether/sse.py` - SSE stream generator
