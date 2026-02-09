# Session Engine

The session engine manages the lifecycle of supervised agent runs — creation, state transitions, event emission, and cleanup.

## Architecture

See [Architecture](ARCHITECTURE.md) for visual diagrams of the full system, event flow, and interaction loop.

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

## Event Distribution

`store.emit()` broadcasts every event to **all** subscriber queues for that session. Two independent consumers attach to these queues:

See the [Event Flow](ARCHITECTURE.md#event-flow) and [Interaction Loop](ARCHITECTURE.md#interaction-loop) diagrams for a visual version of this.

```
                              ┌─── SSE stream ──────── Web UI (Vue)
                              │    (raw passthrough)    renders all events client-side
store.emit() ── subscriber ───┤
              queues          │
                              └─── BridgeSubscriber ── Messaging bridges
                                   (filtered)          server-side rendering
```

**SSE (Web UI path):** The `/api/events/sessions/{id}` endpoint calls `store.new_subscriber()`, replays historical events from the JSONL log, then enters a live loop forwarding every event as-is. No filtering, no interpretation — the Vue app decides what to render. All event types are delivered: intermediate output, thinking steps, tool calls, permission requests, state changes, heartbeats.

**BridgeSubscriber (Messaging path):** Also calls `store.new_subscriber()` — same mechanism. But `_consume()` applies heavy filtering before calling bridge methods: only `final=True` output is forwarded, history events are skipped, intermediate steps are dropped. The bridge base class then handles server-side formatting, auto-approve logic, and error debouncing before sending to the platform.

**Why the web UI is not a bridge:** Bridges are server-side event consumers that filter, interpret, and render for text-based messaging platforms. The web UI is a raw event passthrough where all intelligence lives in the Vue frontend. The bridge abstraction (auto-approve state machines, text command parsing, thread management, platform-specific formatting) doesn't apply to a rich client that receives the full event stream. The shared layer is the store subscriber queue — and that's already cleanly abstracted.

A session can be consumed by the web UI and zero or one messaging bridge simultaneously. The web UI always works regardless of platform binding. The `platform` field on a session determines which bridge (if any) subscribes.

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
