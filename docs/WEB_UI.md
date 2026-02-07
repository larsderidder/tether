# Web UI

Vue 3 mobile-first PWA served by the agent in production. Connects via REST + SSE.

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
