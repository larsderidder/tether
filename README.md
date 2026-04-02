# Tether

The open source supervision layer for local AI coding agents.

You run Claude Code, Codex, OpenCode, or Pi in your terminal. Tether sits alongside and lets you watch and control those sessions from anywhere: a mobile-first web UI, or Telegram, Slack, and Discord threads with live output and approval buttons.

```
Claude Code / Codex / OpenCode / Pi
          |
          v
      Tether (local)
        |             |
        v             v
   Web UI (PWA)   Telegram / Slack / Discord
```

## The typical workflow

You already have an agent running. Tether attaches to it:

```bash
pipx install tether-ai
tether init          # generates a token, optionally sets up a bridge
tether start         # runs in the background

tether attach        # pick from sessions running in the current directory
```

From that point the session appears in the web UI and, if you set up a bridge, gets its own Telegram topic or Discord thread where output streams live and approvals show up as buttons.

If you want to launch agents through Tether rather than directly:

```bash
# Set a default adapter once
echo "TETHER_DEFAULT_AGENT_ADAPTER=claude_auto" >> ~/.config/tether/config.env

tether new .                          # create a session in the current directory
tether new . -m "fix the failing tests"  # create and start immediately
```

## What you get

- Every session in one place: state, output, diffs, approvals
- Approval prompts as buttons in Telegram, Slack, or Discord, not terminal popups
- Auto-approve rules for low-risk tool patterns
- Human in the loop gates you can configure per session
- CLI client for scripting: `tether list`, `tether input`, `tether interrupt`, and more
- MCP server and REST API for custom agents

## Install

```bash
pipx install tether-ai
```

Bridge dependencies are optional extras:

```bash
pip install tether-ai[telegram]   # or [slack] or [discord]
```

Node.js is required for the Codex and OpenCode adapters. The sidecar bundles are included in the package and started automatically.

## Setup

```bash
tether init    # generates ~/.config/tether/config.env
tether start
```

Open `http://localhost:8787`.

`tether init` generates an auth token and optionally walks you through a messaging bridge. That is all it does. You do not need to configure an agent adapter to get started; just run your agents as usual and attach them.

### From source

```bash
git clone https://github.com/larsderidder/tether.git
cd tether
make install
cp .env.example .env
make start
```

Release package assets are built by `.github/workflows/release-assets.yml`. That
workflow emits a Debian package, a Homebrew formula, and a bundled wheelhouse
artifact so fleet installs can pin the exact published build.

## Attaching external sessions

Tether discovers sessions from Claude Code, Codex, OpenCode, and Pi that are already running on your machine. Use the external session browser in the web UI, or from the CLI:

```bash
tether list --external            # show all discoverable sessions
tether attach                     # pick from sessions in current directory
tether attach <id-prefix>         # attach by ID (prefix is fine)
tether attach <id> -p telegram    # attach and create a Telegram topic
```

After attaching, use `tether sync` to pull messages that arrived before attachment.

## Messaging bridges

Configure credentials in `~/.config/tether/config.env` (or `.env` in the project root). The bridge starts automatically with `tether start`.

| Platform | Required vars |
|----------|--------------|
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_FORUM_GROUP_ID` |
| Slack    | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL_ID` |
| Discord  | `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` |

Telegram requires a supergroup with Topics enabled. Each session gets its own topic. Commands work in the General topic: `/list`, `/attach`, `/new`, `/help`.

## Adapters

Set `TETHER_DEFAULT_AGENT_ADAPTER` to use `tether new` or create sessions from the UI without specifying an adapter each time. If you only ever attach external sessions, you do not need this.

| Adapter | Description |
|---------|-------------|
| `claude_auto` | Claude Code, auto-detects OAuth or API key |
| `claude_subprocess` | Claude via Agent SDK subprocess |
| `opencode` | OpenCode via TypeScript sidecar (auto-managed) |
| `codex_sdk_sidecar` | Codex via TypeScript sidecar |
| `pi_rpc` | Pi coding agent via JSON-RPC |
| `litellm` | Any model via LiteLLM (experimental) |

## CLI

```bash
tether init                          # setup wizard
tether start                         # start the server
tether start --dev                   # dev mode (no auth)
tether start --port 9000

# attach to agents already running on your machine
tether list --external               # discover Claude Code / Codex / OpenCode / Pi sessions
tether attach                        # pick from sessions in current directory
tether attach <id>                   # attach by ID prefix
tether attach <id> -p telegram       # attach and bind a messaging thread

# manage Tether sessions
tether status                        # server health and active bridges
tether list                          # list Tether sessions
tether list -s running               # filter by state
tether new [directory]               # create a new session
tether new . -a opencode -m "..."    # create and start with a prompt
tether input <id> "message"          # send input
tether interrupt <id>                # interrupt a running session
tether sync <id>                     # pull new messages from an attached session
tether watch <id>                    # stream live output to the terminal
tether delete <id>                   # delete a session
tether open                          # open web UI in browser
```

Session IDs accept short prefixes.

## External Agent API

Any agent can connect to Tether via MCP or REST to get supervision without being one of the built-in adapters.

### MCP

```bash
tether-mcp
```

Tools: `create_session`, `send_output`, `request_approval`, `check_input`.

```json
{
  "mcpServers": {
    "tether": { "command": "tether-mcp" }
  }
}
```

Install: `pip install tether-ai[mcp]`

### REST

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
```

See `docs/API_REFERENCE.md` for full endpoint documentation.

## Configuration

Config is loaded in order: environment variables, local `.env`, `~/.config/tether/config.env`.

```bash
TETHER_AGENT_TOKEN=               # auth token (required in non-dev mode)
TETHER_DEFAULT_AGENT_ADAPTER=     # default adapter for new sessions (optional)
TETHER_AGENT_HOST=0.0.0.0         # bind address
TETHER_AGENT_PORT=8787            # port
```

See `.env.example` for the full reference.

## Development

```bash
make install    # Python + Node dependencies
make start      # build UI and start
make dev-ui     # hot-reload UI dev server (run agent separately)
make test       # pytest
make verify     # health check
```

See `AGENTS.md` and `docs/` for architecture documentation.

## Running in the background

```bash
nohup tether start > ~/.local/share/tether/tether.log 2>&1 &
echo $! > ~/.local/share/tether/tether.pid
kill $(cat ~/.local/share/tether/tether.pid)
```

Or use systemd, launchd, etc.

## License

Apache 2.0
