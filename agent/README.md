# <img src="https://raw.githubusercontent.com/larsderidder/tether/main/tether_compact_logo.png" height="32"> Tether

[![CI](https://github.com/larsderidder/tether/actions/workflows/ci.yml/badge.svg)](https://github.com/larsderidder/tether/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/tether-ai)](https://pypi.org/project/tether-ai/)
[![Python](https://img.shields.io/pypi/pyversions/tether-ai)](https://pypi.org/project/tether-ai/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

The open source infra layer between your local coding agents and messaging apps.

Tether runs on your machine and turns agent runs into something you can *supervise* from anywhere:
a mobile friendly web UI plus messaging bridges (Telegram, Slack, Discord) with approvals, input
prompts, and live output streaming.

If you're running Claude Code, Codex, OpenCode, or Pi locally and you want supervision (logs, state, diffs, approvals) in the places you
already work, this is that layer.

```
Claude Code / Codex / OpenCode / Pi / custom agent
          |   (adapter, MCP, or REST)
          v
      Tether (local control plane)
        |             |
        v             v
   Web UI (PWA)   Telegram/Slack/Discord
```

## How it works

```
1. Start Tether on your machine (or a VM you control)
2. Open the web UI, or connect a messaging bridge
3. Run an agent session (Claude / Codex / custom)
4. Stream output + state in real time (web + messaging threads)
5. Approve tool use, provide input, or interrupt when needed
```

## What you get

1. A single place to observe every agent session (state, logs, diffs, approvals)
2. Messaging native control: per session threads with approve and reject controls
3. Human in the loop gates for risky operations (file writes, shell commands, etc.)
4. A stable interface for agents: run via built in adapters, or connect via MCP or REST

## Features

1. Local first: runs on your machine, your data stays yours
2. Human in the loop: approve tool use, provide input, review diffs
3. Observable: live streaming output and explicit session state (web and messaging)
4. Messaging bridges: Telegram, Slack, and Discord with approvals and auto approve
5. Multi adapter: Claude Code (OAuth or API key), Codex via sidecar, OpenCode via sidecar, Pi coding agent, plus LiteLLM (experimental)
6. External agent API: MCP server and REST API for custom agents and integrations
7. Mobile first UI: PWA dashboard for monitoring and controlling sessions (experimental)
8. CLI client: manage sessions, attach external agents, and send input from your terminal

## Quick Start

```bash
pipx install tether-ai
tether init
tether start
```

Then open `http://localhost:8787`.

The `init` wizard generates an auth token, detects your `claude` CLI, and optionally
configures a messaging bridge. Config is saved to `~/.config/tether/config.env`.

On Android or Termux, prefer `opencode` as the default adapter. The Codex CLI is
not currently a viable default on Android arm:

```bash
echo "TETHER_DEFAULT_AGENT_ADAPTER=opencode" >> ~/.config/tether/config.env
tether start
```

### Typical uses

1. Keep long running agent work accountable: audit output, diffs, and approvals after the fact
2. Run an agent on a workstation or VM and supervise from your phone (web UI or messaging)
3. Put approvals where your team already lives (Slack or Discord) instead of in a terminal
4. Plug in your own agent: use MCP or REST to emit events and request approvals

### From source

```bash
git clone https://github.com/larsderidder/tether.git
cd tether
make install
cp .env.example .env
make start
```

## External Agent API (for your own agents)

Any AI agent can connect to Tether to get human in the loop supervision. Two interfaces
are available. Use whichever fits your agent tooling:

### MCP (recommended for Claude Code and other MCP capable agents)

The MCP server exposes Tether as tools that any agent can call to register a session,
stream output, and request human approval:

```bash
tether-mcp
# or: python -m tether.mcp_server.server
```

Tools: `create_session`, `send_output`, `request_approval`, `check_input`.

Add to your agent's MCP config:
```json
{
  "mcpServers": {
    "tether": {
      "command": "tether-mcp"
    }
  }
}
```

Install: `pip install tether-ai[mcp]`

### REST

For agents that don't support MCP, the same workflow is available via REST:

```bash
# Create a session
curl -X POST http://localhost:8787/api/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "My Task", "agent_type": "custom"}'

# Push output
curl -X POST http://localhost:8787/api/sessions/{id}/events \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type": "output", "data": {"text": "Hello from agent"}}'

# Poll for human input
curl http://localhost:8787/api/sessions/{id}/events/poll?since_seq=0 \
  -H "Authorization: Bearer $TOKEN"
```

See `docs/API_REFERENCE.md` for full endpoint documentation.

## Adapters (built in runners)

Set `TETHER_DEFAULT_AGENT_ADAPTER` in `.env`:

1. `claude_auto`: Auto detect (prefer OAuth, fallback to API key)
2. `claude_subprocess`: Claude via Agent SDK in subprocess (OAuth or API key)
3. `codex_sdk_sidecar`: Codex via TypeScript sidecar
4. `opencode`: OpenCode via TypeScript sidecar (auto-managed, uses the OpenCode SDK)
5. `pi_rpc`: [Pi coding agent](https://github.com/badlogic/pi-mono) via JSON-RPC subprocess
6. `litellm`: Any model via LiteLLM (DeepSeek, Gemini, OpenRouter, etc.), experimental

Sessions can override the default adapter at creation time. Multiple adapters can run simultaneously.

The Codex and OpenCode sidecars share a common infrastructure layer (`sidecar-common`) and are built as
npm workspace packages. When using managed mode (the default for OpenCode), Tether starts and stops the
sidecar process automatically.

> **Note:** The Codex and OpenCode adapters require Node.js on your PATH. The sidecar bundles
> are included in the PyPI package and started automatically.

> **Android / Termux:** use `opencode` as the default adapter unless you have a
> platform-specific reason to do otherwise.

## Messaging Bridges

Connect a messaging platform so you can monitor and control sessions from your phone. Configure
credentials in `.env`. The bridge starts automatically.

Platforms:
1. Telegram: `TELEGRAM_BOT_TOKEN` plus `TELEGRAM_FORUM_GROUP_ID` (supergroup with topics)
2. Slack: `SLACK_BOT_TOKEN` plus `SLACK_APP_TOKEN` plus `SLACK_CHANNEL_ID`
3. Discord: `DISCORD_BOT_TOKEN` plus `DISCORD_CHANNEL_ID`

### How to use it (2 minutes)

1. Set the platform env vars above (or run `tether init` and let it guide you).
2. Start Tether: `tether start`
3. In Telegram: run `/list`, then `/attach <number>`
4. In Slack/Discord: run `!list`, then `!attach <number>`

That creates a per session thread (topic or thread) where output streams live and approvals show up as buttons or text prompts.

Bridge features:
1. Live output streaming to threads (one per session)
2. Approval request controls with approve, reject, and always approve
3. Auto approve with configurable tool patterns and duration
4. Session listing, status updates, and input forwarding

Install bridge dependencies:
```bash
pip install tether-ai[telegram]   # or [slack] or [discord]
```

## CLI

```
tether init                          # Interactive setup wizard
tether start                         # Start the server
tether start --dev                   # Dev mode (no auth required)
tether start --port 9000 --host 127.0.0.1

# Client commands (talk to a running server)
tether status                        # Server health + session summary
tether open                          # Open web UI in browser
tether new . -a opencode -m "..."    # Android/Termux-friendly new session
tether list                          # List Tether sessions
tether list -s running               # Filter by state
tether list -d .                     # Filter by directory
tether list --external               # Discover Claude Code / Codex / OpenCode / Pi sessions
tether attach <external-id>          # Attach an external session to Tether
tether attach                        # Pick from external sessions in current dir
tether attach <id> -p discord        # Attach and create a Discord thread
tether input <session-id> "message"  # Send input to a session
tether interrupt <session-id>        # Interrupt a running session
tether delete <session-id>           # Delete a session
tether sync <session-id>             # Pull new messages from an attached external session
```

Session IDs accept short prefixes (first few characters are enough as long as they are unique).

Codex external sessions can be discovered and attached for history sync and bridge
mirroring before the sidecar is reachable. Starting or resuming Codex execution
still requires the sidecar backend to be running.

### Running in the background

Tether does not have a built in daemon mode. To run it in the background:

```bash
nohup tether start > ~/.local/share/tether/tether.log 2>&1 &
echo $! > ~/.local/share/tether/tether.pid

# Later, to stop it:
kill $(cat ~/.local/share/tether/tether.pid)
```

Or use your system's service manager (systemd, launchd, etc.).

## Local Runner Shortcuts

```bash
make start-codex
make start-opencode
```

`make start-opencode` is the direct local entrypoint for the managed OpenCode sidecar.

## Configuration

Tether loads config from layered sources (highest precedence first):

1. Environment variables
2. Local `.env` file (working directory)
3. `~/.config/tether/config.env` (created by `tether init`)

Key settings:

```bash
TETHER_DEFAULT_AGENT_ADAPTER=claude_auto  # Agent adapter
TETHER_AGENT_TOKEN=               # Protect the API/UI with bearer auth
TETHER_AGENT_HOST=0.0.0.0         # Bind address (default: 0.0.0.0)
TETHER_AGENT_PORT=8787            # Port (default: 8787)
```

See `.env.example` for the complete reference including adapter-specific settings, session
timeouts, logging, and bridge configuration.

## Development

```bash
make install    # Install Python + Node dependencies
make start      # Build UI and run agent
make dev-ui     # Run UI dev server (hot reload); run agent separately
make test       # Run pytest
make verify     # Health check
```

See `AGENTS.md` for full developer docs and `docs/` for architecture documentation.

## License

Apache 2.0
