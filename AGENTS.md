# AI Agent Instructions

This file is the canonical entrypoint for AI agents working in this repository.

## What is Tether

Local-first control plane for supervising AI coding agents. Start agents (Claude Code, Codex), monitor progress, review changes, approve actions — from mobile, browser, or messaging platforms (Telegram, Slack, Discord).

## Before You Start

### If you're helping a user set up Tether

Guide them through setup:
1. `make install` to install dependencies
2. `cp .env.example .env` and configure
3. `make start` to run (or `make start-codex` for Codex)
4. `make verify` to check everything works
5. Open `http://localhost:8787` — see phone access section below

### If you're developing on the codebase

Read the relevant docs below based on what you'll be working on.

---

## Documentation Guide

### Always read (shared references)

| Document | What it covers |
| --- | --- |
| `docs/DATA_MODEL.md` | Session model, states, event types, runtime state |
| `docs/API_REFERENCE.md` | All REST endpoints, SSE event types, auth |

### Read based on what you're working on

| Area | Document | When to read |
| --- | --- | --- |
| Session engine | `docs/SESSION_ENGINE.md` | Store, state machine, event pipeline, locking |
| Bridges | `docs/BRIDGES.md` | Telegram/Slack/Discord, subscriber routing, auto-approve |
| Runners | `docs/RUNNERS.md` | Runner protocol, adapters (claude_local, codex, claude_api) |
| Web UI | `docs/WEB_UI.md` | Vue 3 frontend, views, composables, dev server |
| MCP server | `docs/MCP_SERVER.md` | MCP tools, transport, config |
| Code standards | `docs/CODE_STANDARDS.md` | Formatting, typing, logging conventions |

### Historical / product context (optional)

| Document | Purpose |
| --- | --- |
| `background/GOAL.md` | Project philosophy and success criteria |
| `background/ROADMAP.md` | Original development phases |
| `background/PRODUCT_STATEMENT.md` | Product positioning |

---

## Project Layout

```
agent/                  # Python backend
  tether/
    api/                # FastAPI routes (sessions, events, directories, deps)
    runner/             # Execution adapters (claude_local, claude_api, codex_*)
    bridges/            # Messaging bridges (telegram, slack, discord)
    mcp/                # MCP server (tools, transport)
    models.py           # Session + event models
    store.py            # Session store + JSONL event log
    main.py             # App entrypoint
  tests/                # pytest test suite
ui/                     # Vue 3 mobile-first PWA
  src/
    views/              # Page components
    composables/        # Shared logic (useSession, useSSE, etc.)
background/             # Specs, docs, plans (not runtime code)
```

---

## Dev Commands

```bash
make install            # Install Python + Node dependencies
make start              # Start agent (Claude adapter)
make start-codex        # Start agent (Codex adapter)
make dev                # Start with hot reload (agent + UI)
make test               # Run pytest
make verify             # Health check agent + UI
make lint               # Run Black formatter check
```

## Running Tests

```bash
cd agent && python -m pytest tests/ -v          # All tests
cd agent && python -m pytest tests/test_foo.py  # Single file
```

---

## Git Commit Policy

- **Single-line** commit messages (no multi-line body)
- **Sentence case** (e.g., "Add feature" not "add feature")
- Concise and descriptive

Examples:
```
Add settings module tests
Fix token validation in auth middleware
Refactor bridge subscriber routing
```

## Code Standards

- **Python**: Black formatter (`cd agent && python -m black .`)
- **Type hints**: Modern syntax (`list`, `dict`, `str | None`)
- **Logging**: structlog with request identifiers
- **Docstrings**: For non-trivial logic; keep concise

See `docs/CODE_STANDARDS.md` for full details.

---

## Phone Access

To access from a phone on the same network:
1. Find the computer's IP address
2. Open firewall port 8787:
   - **Linux (ufw):** `sudo ufw allow 8787/tcp`
   - **Linux (firewalld):** `sudo firewall-cmd --add-port=8787/tcp --permanent && sudo firewall-cmd --reload`
   - **macOS:** System Settings > Network > Firewall > Allow incoming
3. Open `http://<ip>:8787` on phone

## Docker Alternative

```bash
make docker-start
```

Map host directories in `docker-compose.yml` for file system access. Native setup (`make start`) is recommended.

---

## Design Principles

- Local-first — runs on your machine, no cloud
- Human-in-the-loop — AI is supervised, not autonomous
- Observable over magical — visible logs, explicit diffs
- Explicit over implicit
- Simple over clever
