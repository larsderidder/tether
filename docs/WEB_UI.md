# Web UI

Vue 3 mobile-first PWA served by the agent in production. Connects via REST + SSE. See [Architecture](ARCHITECTURE.md) for visual diagrams of how the web UI fits in the overall system.

## Stack

- **Vue 3** + TypeScript + Composition API
- **Vite** for dev server and build
- **Tailwind CSS** + shadcn-vue components
- **Pinia** (or reactive state) for state management

## Architecture

```
Vue App
  ├── Views (SessionDetail, ActiveSession, Settings)
  ├── Components (ChatMessageList, MessageBubble, InputBar, DiffViewer, ...)
  ├── Composables (useAuth, useSessions, useSessionGroups, useDirectoryCheck, ...)
  └── API layer (api.ts, state.ts)
         |
         ├── REST calls to /api/*
         └── SSE stream from /api/events/sessions/{id}
```

## Event Subscription

The web UI connects to the same store subscriber queue that messaging bridges use — but consumes events differently.

The SSE endpoint (`/api/events/sessions/{id}`) calls `store.new_subscriber()` to get a queue, replays historical events from the JSONL log, then streams every new event as-is. No server-side filtering or interpretation. The Vue app receives all event types (output, thinking, tool calls, permissions, state changes, heartbeats) and decides what to render.

This is the opposite of bridges, which filter heavily server-side (only final output, skip intermediate steps) and format for text-based platforms. See [Session Engine > Event Distribution](SESSION_ENGINE.md#event-distribution) for the full comparison.

For sending input back, the Vue app calls the same REST endpoints that bridges use internally: `POST /api/sessions/{id}/input` for user messages, `POST /api/sessions/{id}/permission` for approval responses.

## Key Views

- **SessionDetail** — Full session history, controls, diff viewer
- **ActiveSession** — Real-time streaming display during RUNNING state
- **Settings** — Configuration and preferences
- **ExternalSessionBrowser** — Discover and attach to Claude Code/Codex sessions

## Key Composables

| File | Purpose |
|------|---------|
| `useAuth.ts` | Bearer token auth state |
| `useSessions.ts` | Session list polling + SSE streaming |
| `useSessionGroups.ts` | Session grouping/filtering for sidebar |
| `useDirectoryCheck.ts` | Directory path validation |
| `useClipboard.ts` | Clipboard operations |
| `formatters.ts` | Date/time formatting |

## Dev Mode

```bash
cd ui && npm run dev
```
Vite proxies `/api` and `/events` to the agent (port 8787).

## Build

```bash
make build-ui
```
Builds and copies to `agent/static_ui/` for same-origin serving.

## Key Files

- `ui/src/App.vue` — Root component
- `ui/src/views/` — Page-level views
- `ui/src/components/` — Reusable components
- `ui/src/composables/` — State + API hooks
- `ui/src/api.ts` — REST client
- `ui/src/state.ts` — Reactive state
- `ui/vite.config.ts` — Build config with API proxy

## Design Principles

- Mobile-first: designed for phone screens
- Not a code editor: view-only for code, diffs
- Minimal controls: start, stop, input, approve/deny
- Real-time: SSE streaming for live output
