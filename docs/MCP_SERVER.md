# MCP Server

Thin Model Context Protocol wrapper that exposes Tether's REST API as MCP tools. Allows Claude Desktop and other MCP clients to interact with Tether.

## Tools

| Tool | Description |
|------|-------------|
| `create_session` | Create a new agent session |
| `send_output` | Send output text to a session |
| `request_approval` | Request human approval |
| `check_input` | Poll for pending human input or approval responses |

## Transport

Stdio transport (stdin/stdout JSON-RPC). Entry point: `tether-mcp` or `python -m tether.mcp_server.server`.

## Implementation

Tools are thin wrappers that make HTTP calls to the local Tether agent API.
Debug mode: `TETHER_MCP_DEBUG_IO=1` logs all I/O.

## Config

| Env Var | Description |
|---------|-------------|
| `TETHER_API_URL` | Agent URL (default: http://localhost:8787) |
| `TETHER_AUTH_TOKEN` | Auth token for API calls |

## Key Files

- `agent/tether/mcp_server/server.py` — MCP server entry point
- `agent/tether/mcp_server/tools.py` — Tool definitions + execution

## Tests

- `tests/test_mcp_server.py` — Tool definitions, basic execution
