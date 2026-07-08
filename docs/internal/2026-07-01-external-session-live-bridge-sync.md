# External session live bridge sync

## Problem

Tether can bind a local agent session to a bridge thread, but attached external sessions only update the bridge when the user runs `/sync`. This is especially visible with pi TUI sessions. If Lars keeps working in the TUI, Telegram stays stale until manual sync.

The goal is to make bridges stay current when work happens outside Tether's managed runner process.

## Existing bindings

Tether already has the durable mapping we need:

- `Session.id`: Tether session id, such as `sess_...`
- `Session.runner_session_id`: external runner session id, such as a pi UUID
- `Session.platform`: bridge platform, such as `telegram`, `slack`, or `discord`
- `Session.platform_thread_id`: platform thread or topic id
- `store.find_session_by_runner_session_id(...)`: reverse lookup from external id to Tether session

We should reuse this. No separate bridge binding model is needed.

## Current behavior

`POST /api/sessions/{id}/sync` does this:

1. Resolve `runner_session_id` from the Tether session.
2. Resolve runner type from the session.
3. Load external history via `get_external_session_detail(...)`.
4. Compare against `runtime.synced_message_count`.
5. Emit imported history events for the UI.
6. Directly relay imported messages to the bound bridge.

This works for manual catch-up, but it has two gaps:

- `synced_message_count` is runtime-only, so restart recovery has special replay logic.
- The sync implementation lives inside the API endpoint, so a background watcher would duplicate logic unless we extract it.

## Hybrid approach

Implement this in two layers.

### Layer 1: generic external session watcher

A Tether background service watches attached external sessions and automatically runs the same delta sync as `/sync`.

Scope:

- Works for pi, Claude Code, Codex, and OpenCode through the existing `get_external_session_detail(...)` abstraction.
- Works for any bridge because it operates at the Tether session level, not at Telegram level.
- Initially watches platform-bound sessions only. Later we can also watch unbound sessions for Web UI freshness.

Flow:

1. On startup, discover sessions with both `runner_session_id` and `platform` set.
2. On bridge bind or attach, register the session with the watcher.
3. Every few seconds, check whether the external session changed.
4. If changed, run shared delta sync.
5. Relay new messages to the bound bridge through the same bridge-agnostic path used by `/sync`.

First implementation can poll `get_external_session_detail(...)` directly. That is simple and provider agnostic. Later each provider can expose a cheap version signal such as file mtime, message count, or SQLite updated timestamp.

### Layer 2: optional pi live extension

A pi extension gives true live updates from TUI sessions. It should hook pi lifecycle events and push them to Tether:

- `agent_start`
- `message_update`
- `message_end`
- `tool_execution_start`
- `tool_execution_update`
- `tool_execution_end`
- `agent_end`
- model and thinking level changes if useful later

The extension should identify the current pi session id from the session file, then push to a Tether endpoint keyed by external id. Tether already maps that id back to `Session.id`.

Suggested endpoint:

```http
POST /api/external-sessions/pi/{pi_session_id}/events
```

The endpoint should:

1. Resolve `Session.id` with `store.find_session_by_runner_session_id(pi_session_id)`.
2. Validate the session exists.
3. Translate pi event payloads into Tether events.
4. Reuse `bridge_segments` compatible with `pi_rpc`.

The existing endpoint `POST /api/sessions/{id}/events` can remain, but it needs small improvements if we want to reuse it:

- preserve `bridge_segments` on output events
- support user input mirroring
- support final output in a way compatible with `finalize_output`

The external-id endpoint avoids requiring the pi extension to know Tether's internal `sess_...` id.

## Shared sync service

Extract the manual `/sync` logic into a service, for example `tether.external_sync`.

Proposed public function:

```python
async def sync_external_session_delta(
    session_id: str,
    *,
    force: bool = False,
    source: str = "manual",
) -> SyncResult:
    ...
```

Responsibilities:

- validate the Tether session
- resolve external id and runner type
- fetch external detail
- compute delta using stored cursor
- emit imported messages for Web UI
- relay imported messages to the bound bridge, if any
- update sync cursor

`/sync` becomes a thin wrapper. The watcher calls the same function with `source="watcher"`.

## Cursor model

Start with the current in-memory cursor:

- `runtime.synced_message_count`
- `runtime.synced_turn_count`

This keeps the first change small. It also matches current behavior.

Follow-up improvement: persist the cursor in the database or a small per-session metadata file. That removes restart ambiguity and avoids recovery replay heuristics.

## Dedupe rules

The watcher and the future pi extension can both report the same final assistant message. We need a simple dedupe strategy.

For imported history messages:

- Keep existing event-level duplicate avoidance where possible.
- Compare by external message index while cursor is valid.
- During recovery, avoid appending events when existing imported history is already complete.

When the pi live extension exists:

- Mark live-ingested events with metadata such as `source="pi_live"`.
- Mark watcher-imported events with `source="external_sync"`.
- If the watcher sees a final message that was already emitted live, skip bridge relay and only update the cursor if needed.

Layer 1 can ship without full live-extension dedupe, as long as it does not duplicate its own imports.

## Watcher service design

Create a small lifecycle-managed service, for example `agent/tether/external_session_watcher.py`.

Responsibilities:

- keep one background task
- periodically scan attached sessions
- sync eligible sessions
- handle errors without killing the loop
- support explicit `register(session_id)` and `unregister(session_id)` calls for fast reaction after attach, bind, detach, or delete

Eligibility for first version:

```python
session.runner_session_id and session.platform
```

Suggested settings:

- `TETHER_EXTERNAL_SYNC_WATCHER_ENABLED`, default `true`
- `TETHER_EXTERNAL_SYNC_INTERVAL_SECONDS`, default `3`, bounded to a safe range

The watcher should not import Telegram, Slack, or Discord modules directly. It should call the shared sync service.

## API and bridge changes

Minimal changes:

1. Extract `/sync` logic into `external_sync.py`.
2. Add watcher service and start it from `main.py` lifespan after bridges are initialized.
3. Register sessions when:
   - attaching an external session
   - binding a platform to an existing session
   - startup restores platform-bound sessions
4. Unregister sessions when:
   - deleting a session
   - detaching a platform
5. Keep bridge delivery inside the sync service using existing bridge lookup helpers.

## Tests

Add tests for:

- `/sync` still works through the extracted service.
- Watcher syncs a new external message without calling Telegram-specific code.
- Watcher skips sessions without `runner_session_id`.
- Watcher skips unbound sessions in the first version.
- Watcher errors are logged and do not stop future syncs.
- Existing Codex restart recovery behavior remains unchanged.

## Rollout

Phase 1:

- Extract shared sync service.
- Add generic watcher with polling.
- Wire lifecycle.
- Add tests.

Phase 2:

- Persist sync cursor.
- Add cheap provider change detection.

Phase 3:

- Add pi live extension and external-id ingest endpoint.
- Reuse `pi_rpc` event translation where possible.
- Add dedupe between live events and watcher catch-up.

## Decision

Start with Phase 1. It gives bridge-agnostic automatic catch-up for pi, Claude Code, Codex, and OpenCode without changing any external agent. The pi live extension remains the right next step for true streaming, but it should build on the same binding and dedupe model.
