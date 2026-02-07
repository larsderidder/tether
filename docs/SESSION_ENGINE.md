# Session Engine

The session engine manages the lifecycle of supervised agent runs — creation, state transitions, event emission, and cleanup.

## Architecture

```
REST API  ──>  SessionStore  ──>  SQLite DB (sessions table)
                   |
                   |── SessionRuntime (in-memory per session)
                   |      ├── SSE subscriber queues
                   |      ├── Runner process ref
                   |      ├── Output buffer (dedup)
                   |      ├── Pending permissions (futures)
                   |      └── Seq counter
                   |
                   └── Event Log (JSONL per session)
```

## State Machine

Transitions enforced in `api/state.py` via `transition()` with per-session async locks (`session_lock()`).

Valid transitions defined in `_VALID_TRANSITIONS` dict. Invalid transitions raise HTTP 409.

Key rules:
- `CREATED` can only go to `RUNNING`
- `RUNNING` can go to `AWAITING_INPUT`, `INTERRUPTING`, or `ERROR`
- `ERROR` can go back to `RUNNING` (restart)
- `INTERRUPTING` resolves to `AWAITING_INPUT` or `ERROR`

## Event Pipeline

1. Runner produces events via `RunnerEvents` protocol callbacks
2. `ApiRunnerEvents` (in `api/runner_events.py`) bridges these to SSE:
   - Updates session state/timestamps
   - Calls `emit_*` helpers in `api/emit.py`
3. `emit_*` helpers call `store.emit()` which:
   - Broadcasts to all SSE subscriber queues
   - Appends to JSONL event log on disk
4. `BridgeSubscriber` also consumes from these queues → forwards to messaging bridges

## Session Store (`store.py`)

Key methods:
- `create_session()` / `get_session()` / `update_session()` / `delete_session()` - CRUD
- `emit()` - Broadcast event to subscribers + persist
- `new_subscriber()` / `remove_subscriber()` - SSE queue management
- `add_pending_permission()` / `resolve_pending_permission()` - Approval futures
- `session_usage()` - Aggregate tokens/cost from metadata events
- `next_seq()` - Monotonic sequence counter
- `should_emit_output()` - Output deduplication

## Session Locking

Per-session asyncio locks prevent concurrent state mutations. Used by start, input, interrupt, and delete endpoints to ensure atomic state transitions.

## Key Files

- `agent/tether/store.py` - SessionStore (core state management)
- `agent/tether/models.py` - Session, Message models
- `agent/tether/api/state.py` - State machine, session locks
- `agent/tether/api/runner_events.py` - Runner → SSE bridge
- `agent/tether/api/emit.py` - SSE event emission helpers
- `agent/tether/api/sessions.py` - Session REST endpoints
- `agent/tether/sse.py` - SSE stream generator (replay + live)
- `agent/tether/db/` - SQLite + Alembic migrations
- `agent/tether/maintenance.py` - Idle session cleanup

## Tests

- `tests/test_store.py` - CRUD, sequences, bookkeeping
- `tests/test_state.py` - State transitions (valid + invalid)
- `tests/test_api.py` - Full endpoint coverage
- `tests/test_runner_events.py` - All RunnerEvents callbacks
- `tests/test_session_usage.py` - Usage aggregation
