# AI Agent Instructions

This file is the canonical entrypoint for AI agents working in this repository.

## What is Tether

Local-first control plane for supervising AI coding agents. Start agents (Claude Code, Codex), monitor progress, review changes, approve actions — from mobile or messaging platforms (Telegram, Slack, Discord).

## Before You Start

### If you're helping a user set up Tether

Recommended path (installed via pipx/pip):
1. `tether init` — interactive wizard (generates token, detects adapters, configures bridges)
2. `tether start` — starts the server
3. Connect via Telegram, Slack, or Discord, or use the CLI

From source:
1. `make install` to install dependencies
2. `cp .env.example .env` and configure
3. `make start` to run (or `make start-codex` for Codex)
4. `make verify` to check everything works

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
| Runners | `docs/RUNNERS.md` | Runner protocol, adapters (claude_subprocess, codex, litellm, etc.) |
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
    runner/             # Execution adapters (claude_subprocess, codex_*, litellm, etc.)
    bridges/            # Messaging bridges (telegram, slack, discord)
    mcp/                # MCP server (tools, transport)
    cli.py              # CLI entry point (tether start, tether init)
    config.py           # Layered .env file loader
    init_wizard.py      # Interactive setup wizard
    models.py           # Session + event models
    store.py            # Session store + JSONL event log
    main.py             # App entrypoint
  tests/                # pytest test suite
background/             # Specs, docs, plans (not runtime code)
```

---

## Dev Commands

```bash
make install            # Install Python dependencies
make start              # Build sidecars and start agent
make start-codex        # Start codex-sdk-sidecar and run agent
make test               # Run pytest
make verify             # Health check agent
```

### CLI (installed package)

```bash
tether init                          # Interactive setup wizard
tether start                         # Start server (loads ~/.config/tether/config.env)
tether start --dev                   # Dev mode (no auth)
tether start --port 9000

# Client commands (talk to running server)
tether status                        # Server health + session summary
tether list                          # List Tether sessions
tether list -s running               # Filter by state
tether list -d .                     # Filter by directory
tether list --external               # Discover external sessions
tether new [directory]               # Create a new session
tether new . -a opencode -m "fix tests"  # Create and start immediately
tether attach <external-id>          # Attach an external session
tether attach                        # Pick from sessions in current dir
tether attach <id> -p discord        # Attach with Discord thread
tether input <session-id> "message"  # Send input to a session
tether interrupt <session-id>        # Interrupt a running session
tether delete <session-id>           # Delete a session
tether sync <session-id>             # Pull new messages from an attached external session
tether watch <session-id>            # Stream live output to the terminal

# Remote server
tether -H my-server list             # Connect to a remote Tether server
tether --server work list            # Use a named server from ~/.config/tether/servers.yaml
```

Config precedence: env vars > local `.env` > `~/.config/tether/config.env`.

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

## Docker

Docker support has been removed. Tether is designed to run locally so it can
attach to existing sessions and operate directly on your filesystem.

---

## Design Principles

- Local-first — runs on your machine, no cloud
- Human-in-the-loop — AI is supervised, not autonomous
- Observable over magical — visible logs, explicit diffs
- Explicit over implicit
- Simple over clever
