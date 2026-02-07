# Data Model Reference

Canonical reference for Tether's core data structures. All agents should read this.

## Session (SQLModel table: `sessions`)

The central entity. Represents one supervised agent run.

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` (PK) | `sess_` prefix + random hex |
| `repo_id` | `str` | Repository identifier or `"external"` for attached sessions |
| `repo_display` | `str` | Human-readable repo name |
| `repo_ref_type` | `str` | `"path"` or `"url"` |
| `repo_ref_value` | `str` | Local path or git URL |
| `state` | `SessionState` | Current lifecycle state |
| `name` | `str?` | Auto-set from first prompt (truncated) |
| `created_at` | `str` | ISO8601 UTC |
| `started_at` | `str?` | When first started |
| `ended_at` | `str?` | When terminated |
| `last_activity_at` | `str` | Updated on every output/metadata/heartbeat |
| `exit_code` | `int?` | Runner exit code (non-zero = error) |
| `summary` | `str?` | Optional session summary |
| `runner_header` | `str?` | Runner title/version (e.g., "Claude Code 1.0.3") |
| `runner_type` | `str?` | `"claude"`, `"codex"`, etc. |
| `runner_session_id` | `str?` | External thread ID for resume (unique) |
| `directory` | `str?` | Working directory path |
| `directory_has_git` | `bool` | Whether directory has `.git` |
| `workdir_managed` | `bool` | Whether Tether manages the workdir |
| `approval_mode` | `int?` | `None` = global default, `0/1/2` = override |
| `adapter` | `str?` | Runner adapter name (immutable after creation) |
| `external_agent_id` | `str?` | External agent identifier |
| `external_agent_name` | `str?` | e.g., "Claude Code" |
| `external_agent_type` | `str?` | Agent type string |
| `external_agent_icon` | `str?` | Emoji icon |
| `external_agent_workspace` | `str?` | Agent's workspace path |
| `platform` | `str?` | `"telegram"`, `"slack"`, `"discord"` |
| `platform_thread_id` | `str?` | Platform-specific thread/topic ID |

## Session States

```
CREATED ──> RUNNING <──> AWAITING_INPUT
               |
               v
          INTERRUPTING
               |
               v
          AWAITING_INPUT

ERROR <── (from RUNNING, INTERRUPTING)
ERROR ──> RUNNING (restart)
```

- `CREATED` - Session exists, not started
- `RUNNING` - Agent turn in progress
- `AWAITING_INPUT` - Turn complete, waiting for user
- `INTERRUPTING` - Interrupt requested, aborting turn
- `ERROR` - Terminated with error (can restart)

Transitions enforced in `agent/tether/api/state.py`.

## Message (SQLModel table: `messages`)

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` (PK) | Random ID |
| `session_id` | `str` (FK) | Parent session |
| `role` | `str` | `"user"` or `"assistant"` |
| `content` | `str?` | Message text |
| `created_at` | `str` | ISO8601 UTC |
| `seq` | `int` | Ordering sequence |

## Event Log (JSONL per session)

Events persisted to `{data_dir}/sessions/{session_id}/events.jsonl`. Each line:

```json
{"session_id": "sess_...", "ts": "...", "seq": 12, "type": "output", "data": {...}}
```

Event types: `output`, `output_final`, `session_state`, `metadata`, `heartbeat`, `error`, `warning`, `input_required`, `user_input`, `permission_request`, `permission_resolved`, `header`.

## Runtime State (in-memory)

`SessionRuntime` in `store.py` — per-session volatile state:
- `subscribers` - SSE subscriber queues
- `process` - Runner process reference
- `output_buffer` - Recent output for dedup
- `pending_permissions` - Pending approval futures
- `seq_counter` - Monotonic event sequence
- `runner_session_id` - External thread ID (cached)

## External Session Models (Pydantic, not persisted)

- `ExternalSessionSummary` - Discovered Claude Code/Codex session (id, directory, first_prompt, is_running)
- `ExternalSessionDetail` - Full history with messages
- `ExternalRunnerType` - `CLAUDE_CODE` or `CODEX`

## Key Files

- `agent/tether/models.py` - All model definitions
- `agent/tether/store.py` - SessionStore + SessionRuntime
- `agent/tether/db/` - SQLite engine + Alembic migrations
- `agent/tether/api/state.py` - State machine transitions
