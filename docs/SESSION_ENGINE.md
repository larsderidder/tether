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
   - Appends to the JSONL event log on disk before live publication
   - Offers the event to bounded SSE subscriber queues without waiting on full queues
   - Drops the oldest queued live event, not the newest, when a subscriber falls behind
4. SSE streams and `BridgeSubscriber` replay JSONL sequence gaps after reconnect or queue overflow; bridge deliveries then flow through durable outbox rows

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

**SSE (Web UI path):** The `/api/events/sessions/{id}` endpoint calls `store.new_subscriber()`, replays historical events from the JSONL log, then enters a live loop forwarding every event as-is. No filtering, no interpretation. The Vue app decides what to render. Live queues are bounded; if a client falls behind, overflow keeps the newest event as a wakeup signal and the stream replays the missing sequence range from the persisted log. Reconnecting with `since_seq` uses the same durable log.

**BridgeSubscriber (Messaging path):** Registers a bounded live queue and a durable `bridge_replay_cursors` baseline for each session/platform binding. New bindings start at the current event-log tail, without automatically replaying old web, CLI, or pre-outbox output. Existing bindings recover events after their durable cursor; sequence gaps trigger replay from the retained log.

Live consumption and durable replay progress are separate. Buffered events advance the live sequence so they are not consumed twice, but cannot advance the durable cursor until their output is persisted. A routed event that produces several deliveries is checkpointed only after all its rows have been enqueued. Delayed error status also holds back the durable cursor until persisted or superseded.

Imported history is enqueued before its absolute source-message checkpoint advances, including when the configured platform is temporarily unavailable. Explicit attachment backfill remains bounded to recent history. Permission prompts without a recoverable live waiter are stale and are not resent after restart.

**Why the web UI is not a bridge:** Bridges are server-side event consumers that filter, interpret, and render for text-based messaging platforms. The web UI is a raw event passthrough where all intelligence lives in the Vue frontend. The bridge abstraction (auto-approve state machines, text command parsing, thread management, platform-specific formatting) doesn't apply to a rich client that receives the full event stream. The shared layer is the store subscriber queue — and that's already cleanly abstracted.

A session can be consumed by the web UI and zero or one messaging bridge simultaneously. The web UI always works regardless of platform binding. The `platform` field on a session determines which bridge (if any) subscribes.

## Session Store (`store.py`)

Key methods:
- `create_session()` / `get_session()` / `update_session()` / `delete_session()` - CRUD
- `emit()` - Persist event log entries, then publish to bounded subscriber queues
- `new_subscriber()` / `remove_subscriber()` - Bounded SSE queue management
- `add_pending_permission()` / `resolve_pending_permission()` - Approval futures
- `session_usage()` - Aggregate tokens/cost from metadata events
- `next_seq()` - Monotonic sequence counter
- `should_emit_output()` - Output deduplication

Bridge delivery is strict FIFO per session/platform. Consumers claim only the oldest unsent row, so a failed row in backoff or an active send lease blocks later rows until it is sent, cancelled, or retryable. Bridge delivery is at least once, not exactly once. If Tether crashes after a platform accepted a message but before the outbox row is marked `sent`, the row can be retried after startup and the platform may receive a duplicate. The reverse crash point, persisted row before platform send, is recovered by outbox retry.
## Retention

Inactive sessions are retained for 30 days by default. Set
`TETHER_AGENT_SESSION_RETENTION_DAYS` to override this, or `0` to disable pruning.
Running and interrupting sessions are never pruned. The cutoff uses `ended_at`,
then `last_activity_at`, then `created_at`, taking the first available timestamp.

A separate background task closes recorded Telegram topics whose sessions have
been removed, preserving their chat history. Existing idle and detached sessions
are not orphaned. Session event-log directories are currently retained after
pruning; this retention setting does not reclaim those files.

## Startup and Interrupt Recovery

On FastAPI lifespan startup, Tether reconciles persisted `RUNNING` and `INTERRUPTING` sessions before background watchers start. A persisted `RUNNING` session is kept running only when Tether still owns a live process handle, an attached external session is reported active by its discovery backend, or the session's runner recovers it through its lifecycle hook. Otherwise the session is moved to `ERROR` so the UI does not show work as active without a functioning runner.

`INTERRUPTING` recovery uses the timestamp of the persisted transition into `INTERRUPTING`, not `last_activity_at`, because runner output and heartbeats may continue updating activity while an interrupt is in progress. When an interrupt times out, Tether attempts to dispose the owned runner process outside the session lock before moving the session to `AWAITING_INPUT`. If disposal fails, the session moves to `ERROR` instead of reporting false readiness. Attached external sessions are never killed by this recovery path.

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
