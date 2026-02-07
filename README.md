# <img src="tether_compact_logo.png" height="32"> Tether

[![CI](https://github.com/larsderidder/tether/actions/workflows/ci.yml/badge.svg)](https://github.com/larsderidder/tether/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Early_Development-orange.svg)]()

Your AI coding agents, in your pocket.

Tether is a local-first control plane for supervising AI work. It runs on your machine, serves a
mobile-friendly web UI, and lets you monitor sessions, review output/diffs, and intervene when an
agent needs input — from your phone, browser, or messaging platform.

## How it works

```
1. Start Tether on your machine (or VM)
2. Open the web UI, or connect via Telegram / Slack / Discord
3. Start a session (Claude or Codex)
4. Watch logs and state in real time
5. Approve tool use, provide input, or interrupt when needed
```

## Features

- **Local-first** — runs on your machine, your data stays yours
- **Human-in-the-loop** — approve tool use, provide input, review diffs
- **Observable** — live streaming output and explicit session state
- **Multi-adapter** — Claude (local OAuth or API key) and Codex via sidecar
- **Messaging bridges** — Telegram, Slack, and Discord with approval buttons and auto-approve
- **External agent API** — REST + WebSocket for any agent to connect
- **MCP server** — expose Tether as tools for Claude Desktop and other MCP clients
- **Mobile-first UI** — PWA dashboard for monitoring and controlling sessions

## Quick Start

```bash
git clone https://github.com/larsderidder/tether.git
cd tether
make install
cp .env.example .env
make start
```

Then open `http://localhost:8787`.

Use `make start-codex` to run with the Codex sidecar adapter.

## Adapters

Set `TETHER_AGENT_ADAPTER` in `.env`:

| Adapter | Description |
|---------|-------------|
| `claude_auto` | Auto-detect (prefer OAuth, fallback to API key) |
| `claude_local` | Claude Code via local OAuth |
| `claude_api` | Claude Code via API key |
| `codex_sdk_sidecar` | Codex via sidecar |

Sessions can override the default adapter at creation time. Multiple adapters can run simultaneously.

## Messaging Bridges

Connect a messaging platform so you can monitor and control sessions from your phone. Configure
credentials in `.env` — the bridge starts automatically.

| Platform | What you need |
|----------|---------------|
| **Telegram** | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_GROUP_ID` (supergroup with topics) |
| **Slack** | `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` + `SLACK_CHANNEL_ID` |
| **Discord** | `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` |

Bridge features:
- Live output streaming to threads (one per session)
- Approval request buttons with approve / reject / always-approve
- Auto-approve with configurable tool patterns and duration
- Session listing, status updates, and input forwarding

Install bridge dependencies:
```bash
pip install tether-ai[telegram]   # or [slack] or [discord]
```

## External Agent API

Any AI agent can connect to Tether via REST or WebSocket:

```bash
# Create a session
curl -X POST http://localhost:8787/api/agent/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "My Task", "agent_type": "custom"}'

# Or connect via WebSocket
wscat -c "ws://localhost:8787/api/agent/sessions/{id}/ws?token=$TOKEN"
```

See `docs/API_REFERENCE.md` for full endpoint documentation.

## MCP Server

Expose Tether as MCP tools for Claude Desktop or other MCP clients:

```bash
tether-mcp
# or: python -m tether.mcp.server
```

Tools: `create_session`, `send_output`, `request_approval`, `check_input`.

Install: `pip install tether-ai[mcp]`

## Configuration

Copy `.env.example` to `.env`. Key settings:

```bash
TETHER_AGENT_ADAPTER=claude_auto  # Agent adapter
TETHER_AGENT_TOKEN=               # Protect the API/UI with bearer auth
TETHER_AGENT_HOST=0.0.0.0         # Bind address (default: 0.0.0.0)
TETHER_AGENT_PORT=8787            # Port (default: 8787)
```

See `.env.example` for the complete reference including adapter-specific settings, session
timeouts, logging, and bridge configuration.

## Docker

```bash
docker compose up -d
docker compose --profile codex up -d codex-sidecar
```

Map host directories in `docker-compose.yml` for file system access. Native setup is recommended.

## Development

```bash
make install    # Install Python + Node dependencies
make dev        # Agent + UI with hot reload
make test       # Run pytest
make lint       # Black formatter check
make verify     # Health check
```

See `AGENTS.md` for full developer docs and `docs/` for architecture documentation.

## License

Apache 2.0
