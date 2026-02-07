# Tether Agent

Control your AI coding agents from your phone when you're away from your desk.

You start a coding agent, walk away for lunch, and come back to find it stuck waiting for input for an hour. Tether fixes that. Get notified when your agent needs you, respond from anywhere.

## Features

- **Local-first** — Runs on your machine, your data stays yours
- **Multi-agent** — Supports Claude and Codex, more to come
- **Web UI** — Monitor sessions from your phone or desktop
- **External Agent API** — WebSocket and REST API for connecting any AI agent
- **Messaging Platform Integrations** — Telegram, Slack, and Discord bridges
- **MCP Server** — Model Context Protocol server for Claude Desktop and other MCP clients
- **No API keys required** — Uses Claude / Codex local OAuth by default

## Installation

```bash
pip install tether-ai
```

### Optional Platform Integrations

Install platform-specific dependencies as needed:

```bash
# Telegram bridge
pip install tether-ai[telegram]

# Slack bridge
pip install tether-ai[slack]

# Discord bridge
pip install tether-ai[discord]

# All bridges
pip install tether-ai[telegram,slack,discord]

# Development tools
pip install tether-ai[dev]
```

## Quick Start

### Run the Agent Server

```bash
# Start the agent server
tether-agent
```

Then open http://localhost:8787 in your browser.

### Use with MCP (Claude Desktop)

Add Tether as an MCP server in your Claude Desktop config:

```json
{
  "mcpServers": {
    "tether": {
      "command": "python",
      "args": ["-m", "tether.mcp_server.server"],
      "env": {
        "TETHER_API_URL": "http://localhost:8787"
      }
    }
  }
}
```

## Configuration

Set environment variables to configure:

| Variable | Description | Default |
|----------|-------------|---------|
| `TETHER_AGENT_HOST` | Host to bind to | `0.0.0.0` |
| `TETHER_AGENT_PORT` | Port to listen on | `8787` |
| `TETHER_AGENT_TOKEN` | Auth token (optional; if set, API/UI/MCP require bearer auth) | — |
| `TETHER_AGENT_DEV_MODE` | Enable dev mode (no token required) | `0` |
| `TETHER_AGENT_ADAPTER` | AI adapter to use | `claude_auto` |
| `TETHER_AGENT_DATA_DIR` | Data storage directory | `./data` |

### AI Adapters

| Adapter | Description |
|---------|-------------|
| `claude_auto` | Auto-detect (prefer OAuth, fallback to API key) |
| `claude_local` | Claude via local OAuth |
| `claude_api` | Claude via API key (set `ANTHROPIC_API_KEY`) |
| `codex_sdk_sidecar` | Codex via sidecar |

### Messaging Platform Bridges

Configure bridges to get notifications on your preferred platform:

#### Telegram

```bash
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_GROUP_ID="your_group_id"
```

#### Slack

```bash
export SLACK_BOT_TOKEN="xoxb-your-token"
export SLACK_CHANNEL_ID="C01234567"
```

#### Discord

```bash
export DISCORD_BOT_TOKEN="your_bot_token"
export DISCORD_CHANNEL_ID="1234567890"
```

## External Agent API

Tether exposes a WebSocket and REST API for external agents to connect and interact with users through messaging platforms.

### REST API Endpoints

#### Create a Session

```bash
POST /external/sessions
Content-Type: application/json

{
  "agent_metadata": {
    "name": "My Custom Agent",
    "type": "custom",
    "icon": "🤖",
    "workspace": "my-workspace"
  },
  "session_name": "Code Review Task",
  "platform": "telegram"
}
```

Response:
```json
{
  "session_id": "sess_abc123",
  "platform": "telegram",
  "thread_info": {
    "thread_id": "123456",
    "platform": "telegram"
  }
}
```

#### Send Output

```bash
POST /external/sessions/{session_id}/output
Content-Type: application/json

{
  "text": "Agent output text here",
  "metadata": {}
}
```

#### Request Approval

```bash
POST /external/sessions/{session_id}/approval
Content-Type: application/json

{
  "title": "Approve Changes?",
  "description": "Ready to commit these changes",
  "options": ["Approve", "Reject", "Review"]
}
```

#### Check for Input

```bash
GET /external/sessions/{session_id}/input?timeout=30
```

Response:
```json
{
  "type": "human_input",
  "data": {
    "text": "User's message",
    "timestamp": "2025-01-01T12:00:00Z"
  }
}
```

### WebSocket API

Connect to `/external/sessions/{session_id}/ws` for bidirectional communication:

**Agent → Tether events:**
- `output`: Send text output to user
- `approval_request`: Request user approval
- `status`: Update agent status (thinking, executing, done, error)

**Tether → Agent events:**
- `human_input`: User sent a message
- `approval_response`: User responded to approval request

Example WebSocket message:
```json
{
  "type": "output",
  "data": {
    "text": "Processing your request...",
    "metadata": {}
  }
}
```

## Development

### Run Tests

```bash
pytest tests/
```

### Run with Docker

```bash
docker-compose up
```

### Database Migrations

```bash
# Create a new migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head
```

## Documentation

For full documentation, see [github.com/larsderidder/tether](https://github.com/larsderidder/tether).

## License

Apache 2.0. See [LICENSE](https://github.com/larsderidder/tether/blob/main/LICENSE) for details.
