# MCP Server

Thin Model Context Protocol wrapper that exposes Tether's REST API as MCP tools. Allows Claude Desktop and other MCP clients to interact with Tether.

There are two groups of tools with different use cases:

**Agent self-registration** (agent registers itself with Tether as a session):

| Tool | Description |
|------|-------------|
| `create_session` | Create a new agent session |
| `send_output` | Send output text to a session |
| `request_approval` | Request human approval |
| `check_input` | Poll for pending human input or approval responses |

**Remote agent execution** (orchestrating agent creates and controls other agents):

| Tool | Description |
|------|-------------|
| `run_agent` | Create a session, optionally clone a repo, start an agent with a prompt |
| `get_session_status` | Check state, metadata, and git status of an agent session |
| `get_session_output` | Retrieve output events from an agent session |
| `send_followup` | Send a follow-up message to a session waiting for input |
| `get_diff` | Get the git diff from the agent's workspace |
| `stop_session` | Interrupt a running agent session |

## Orchestrating agents

An orchestrating agent can use Tether as a headless agent execution backend:

1. `run_agent` — start an agent with a repo and task (set `wait: true` to block until the turn completes)
2. `get_session_status` — poll until the session reaches `awaiting_input`
3. `get_diff` — review the changes the agent made
4. `send_followup` — provide corrections or follow-up instructions
5. Repeat until satisfied

`TETHER_API_URL` can point at a remote Tether server, so the controlling agent does not need to be on the same machine.

## Transport

Stdio transport (stdin/stdout JSON-RPC). Entry point: `tether-mcp` or `python -m tether.mcp_server.server`.

## Implementation

Tools are thin wrappers that make HTTP calls to the Tether agent API.
Debug mode: `TETHER_MCP_DEBUG_IO=1` logs all I/O.

## Config

| Env Var | Description |
|---------|-------------|
| `TETHER_API_URL` | Agent URL. Overrides localhost default. Supports remote servers. |
| `TETHER_AGENT_TOKEN` | Auth token for API calls |

When `TETHER_API_URL` is not set, the MCP server connects to `http://localhost:{TETHER_AGENT_PORT}`.

## Key Files

- `agent/tether/mcp_server/server.py` — MCP server entry point
- `agent/tether/mcp_server/tools.py` — Tool definitions + execution

## Tests

- `tests/test_mcp_server.py` — Tool definitions, execution, URL resolution
