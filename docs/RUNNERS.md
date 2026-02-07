# Runners (Agent Adapters)

Runners are execution adapters that start and manage AI agent backends. Each runner implements a common protocol, letting the rest of Tether treat all agents uniformly.

## Runner Protocol (`runner/base.py`)

```python
class Runner(Protocol):
    runner_type: str  # "claude", "codex", etc.
    async def start(session_id, prompt, approval_choice) -> None
    async def send_input(session_id, text) -> None
    async def stop(session_id) -> int | None
    def update_permission_mode(session_id, approval_choice) -> None
```

## RunnerEvents Protocol (`runner/base.py`)

Callbacks from runner → session engine:

| Callback | Purpose |
|----------|---------|
| `on_output(session_id, stream, text, kind, is_final)` | Agent output text |
| `on_header(session_id, title, model, provider, ...)` | Session metadata header |
| `on_error(session_id, code, message)` | Agent error |
| `on_exit(session_id, exit_code)` | Process terminated |
| `on_awaiting_input(session_id)` | Agent needs user input |
| `on_metadata(session_id, key, value, raw)` | Tokens, cost, model info |
| `on_heartbeat(session_id, elapsed_s, done)` | Liveness signal |

Implemented by `ApiRunnerEvents` in `api/runner_events.py` which bridges to SSE events.

## Adapter Implementations

### Claude Local (`runner/claude_local.py`)
- Spawns `claude` CLI subprocess with agent protocol
- Uses Claude Agent SDK (OAuth-based CLI auth)
- Handles tool execution, message history sync
- Adapter name: `claude_local`

### Claude API (`runner/claude_api.py`)
- Direct Anthropic SDK calls (`anthropic.Anthropic`)
- API key auth (`ANTHROPIC_API_KEY`)
- Tool definitions in Anthropic format
- Adapter name: `claude_api`

### Codex SDK Sidecar (`runner/codex_sdk_sidecar.py`)
- REST client to Codex sidecar process (port 8788)
- Session lifecycle via HTTP
- Event streaming
- Adapter name: `codex_sdk_sidecar`

### Auto-detection (`runner/__init__.py`)
`get_runner()` auto-selects adapter:
1. If `TETHER_AGENT_ADAPTER` is set, use that
2. If Claude CLI is available (OAuth), use `claude_local`
3. If `ANTHROPIC_API_KEY` is set, use `claude_api`
4. Raise error

## Runner Registry (`api/runner_registry.py`)

Caches runner instances. `get_runner_registry()` provides global singleton.
`get_api_runner(adapter_name)` returns the runner for a given adapter.

## Config

| Env Var | Description |
|---------|-------------|
| `TETHER_AGENT_ADAPTER` | Force adapter: `claude_local`, `claude_api`, `codex_sdk_sidecar` |
| `ANTHROPIC_API_KEY` | API key for `claude_api` adapter |
| `TETHER_AGENT_CLAUDE_MODEL` | Model override (default: claude-sonnet-4-20250514) |
| `TETHER_AGENT_CODEX_SIDECAR_URL` | Sidecar URL (default: http://localhost:8788) |
| `TETHER_AGENT_CODEX_SIDECAR_TOKEN` | Sidecar auth token |

## Key Files

- `agent/tether/runner/base.py` — Protocol definitions
- `agent/tether/runner/__init__.py` — Auto-detection + factory
- `agent/tether/runner/claude_local.py` — Claude CLI adapter
- `agent/tether/runner/claude_api.py` — Claude API adapter
- `agent/tether/runner/codex_sdk_sidecar.py` — Codex sidecar adapter
- `agent/tether/api/runner_events.py` — RunnerEvents → SSE bridge
- `agent/tether/api/runner_registry.py` — Runner caching

## Tests

- `tests/test_runner_events.py` — All RunnerEvents callbacks
- `tests/test_claude_local.py` — Claude local adapter
- `tests/test_runner_registry.py` — Registry caching
- `tests/test_sidecar_unavailable_error.py` — 503 error handling
