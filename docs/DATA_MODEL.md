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
| `name` | `str?` | Auto-generated summary from the first prompt (truncated) |
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
| `model` | `str?` | Model selected when the session was created |
| `bridge_verbosity` | `str?` | Per-session bridge verbosity override: `none`, `minimal`, `medium`, or `high` |
| `bridge_buffer_max_seconds` | `float?` | Optional per-session max seconds before flushing buffered bridge activity. `None` means flush at final/end turn |
| `synced_message_count` | `int?` | Absolute index into the complete external runner transcript, independent of bounded display history. `null` establishes a baseline without replay on the first sync. |
| `synced_turn_count` | `int?` | Durable count of imported user turns |
| `external_agent_id` | `str?` | External agent identifier |
| `external_agent_name` | `str?` | e.g., "Claude Code" |
| `external_agent_type` | `str?` | Agent type string |
| `external_agent_icon` | `str?` | Emoji icon |
| `external_agent_workspace` | `str?` | Agent's workspace path |
| `platform` | `str?` | `"telegram"`, `"slack"`, `"discord"` |
| `platform_thread_id` | `str?` | Platform-specific thread/topic ID |
| `clone_url` | `str?` | Git URL that was cloned to create this session's workspace |
| `clone_branch` | `str?` | Branch checked out at clone time |
| `working_branch` | `str?` | Working branch created by `auto_branch` (e.g. `tether/a1b2c3`) |

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

Event types: `output`, `output_final`, `session_state`, `metadata`, `heartbeat`, `error`, `warning`, `input_required`, `user_input`, `permission_request`, `permission_resolved`, `header`, `checkpoint`.

### Structured bridge output

`output.data.bridge_segments` is optional metadata for bridge renderers. It is a list of small typed segments with `kind`, `text`, and optional `label` fields. Bridges prefer these segments over parsing marker text, while the UI and old consumers can keep using `output.data.text`.

Supported segment kinds: `assistant`, `thinking`, `tool_call`, `tool_output`, `tool_result`, `tool_error`, `status`, `warning`.
Bridge delivery filters these by the session's effective verbosity before sending to chat platforms.

```json
{
  "text": "[tool: bash]\n",
  "bridge_segments": [
    {"kind": "tool_call", "label": "bash", "text": "{\"command\": \"pwd\"}"}
  ]
}
```

### `checkpoint` event

Emitted after `on_awaiting_input` when `TETHER_GIT_AUTO_CHECKPOINT=true` and the session workspace is a git repository with uncommitted changes.

```json
{
  "commit_hash": "abc1234",
  "message": "Checkpoint: turn 3",
  "files_changed": 2
}
```

## Bridge Delivery Outbox

`bridge_deliveries` stores durable messaging-platform sends. Important fields:

- `delivery_key` - unique idempotency key derived from session, platform, delivery kind, and event sequence
- `status` - `pending`, `sending`, `sent`, or `cancelled`
- `event_seq` - source event sequence when available, used for idempotency and replay correlation
- `delivery_seq` - per-session and per-platform FIFO sequence assigned at enqueue time. This is the authoritative delivery order, including events without `event_seq`.
- `next_attempt_at` / `attempts` / `last_error` - retry bookkeeping

Payloads are bounded before storage. Consumers claim only the FIFO head before sending, then mark it `sent` after the platform call returns. A `sending` row can be reclaimed after its claim deadline, which gives at-least-once recovery across crashes but cannot distinguish a send accepted by the platform from one that failed before acknowledgement.

## Bridge Replay Cursors

`bridge_replay_cursors` stores the durable event-log baseline and cursor for a session/platform binding. A first binding starts at the current maximum event sequence, avoiding automatic replay of legacy output into a newly attached chat. Existing bindings recover events after their cursor on restart. The in-memory consumed sequence can advance while output is buffered, but the durable cursor cannot pass unpersisted buffers, delayed status, or an event whose deliveries are only partly enqueued. Imported transcript checkpoints advance per source message only after its bridge rows and history events are persisted.
## Runtime State (in-memory)

`SessionRuntime` in `store.py` — per-session volatile state:
- `subscribers` - SSE subscriber queues
- `process` - Runner process reference
- `output_buffer` - Recent output for dedup
- `pending_permissions` - Pending approval futures
- `seq_counter` - Monotonic event sequence
- `runner_session_id` - External thread ID (cached)
- `checkpoint_turn_count` - Number of auto-checkpoint commits made (incremented by `next_checkpoint_turn()`)

## Cloned Workspaces

When a session is created with `clone_url`, Tether clones the repository into a
managed workspace under `{data_dir}/workspaces/{session_id}/`.

Key conventions:

| Field | Value |
|-------|-------|
| `repo_ref_type` | `"url"` |
| `repo_ref_value` | The clone URL |
| `workdir_managed` | `True` |
| `directory` | Absolute path to the cloned workspace |
| `clone_url` | Same as `repo_ref_value` (denormalised for API convenience) |
| `working_branch` | Branch created by `auto_branch`, or `None` |

If `auto_branch=True` (or `TETHER_GIT_AUTO_BRANCH=true`), a branch named
`tether/<short_session_id>` is created and checked out immediately after
cloning. The branch name pattern can be changed via `TETHER_GIT_BRANCH_PATTERN`
(e.g. `"work/{session_id}"`).

Workspaces are cleaned up when the session is deleted.

## External Session Models (Pydantic, not persisted)

- `ExternalSessionSummary` - Discovered external session (id, directory, first_prompt, is_running)
- `ExternalSessionDetail` - Full history with messages
- `ExternalRunnerType` - `CLAUDE_CODE`, `CODEX`, `OPENCODE`, or `PI`

## Key Files

- `agent/tether/models.py` - All model definitions
- `agent/tether/store.py` - SessionStore + SessionRuntime
- `agent/tether/db/` - SQLite engine + Alembic migrations
- `agent/tether/api/state.py` - State machine transitions
