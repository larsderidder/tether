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
| `on_permission_request(session_id, request_id, ...)` | Permission request from agent |
| `on_permission_resolved(session_id, request_id, ...)` | Permission resolved |

Implemented by `ApiRunnerEvents` in `api/runner_events.py` which bridges to SSE events.

## Adapter Implementations

### Claude Subprocess (`runner/claude_subprocess.py`)
- Spawns one subprocess per query turn for full process isolation
- Child process (`claude_sdk_worker.py`) runs the Claude Agent SDK
- JSON-line IPC over stdin/stdout
- Uses CLI OAuth auth
- Adapter name: `claude_subprocess`

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
2. If Claude CLI is available (OAuth) + SDK installed, use `claude_subprocess`
3. If `ANTHROPIC_API_KEY` is set, use `claude_api`
4. Raise error

## Runner Registry (`api/runner_registry.py`)

Caches runner instances. `get_runner_registry()` provides global singleton.
`get_api_runner(adapter_name)` returns the runner for a given adapter.

## Config

| Env Var | Description |
|---------|-------------|
| `TETHER_AGENT_ADAPTER` | Force adapter: `claude_subprocess`, `claude_api`, `codex_sdk_sidecar` |
| `ANTHROPIC_API_KEY` | API key for `claude_api` adapter |
| `TETHER_AGENT_CLAUDE_MODEL` | Model override (default: claude-sonnet-4-20250514) |
| `TETHER_CODEX_SIDECAR_URL` | Sidecar URL (default: http://localhost:8788) |
| `TETHER_CODEX_SIDECAR_TOKEN` | Sidecar auth token |

## Key Files

- `agent/tether/runner/base.py` — Protocol definitions
- `agent/tether/runner/__init__.py` — Auto-detection + factory
- `agent/tether/runner/claude_subprocess.py` — Claude subprocess adapter (parent side)
- `agent/tether/runner/claude_sdk_worker.py` — Claude subprocess worker (child side)
- `agent/tether/runner/claude_api.py` — Claude API adapter
- `agent/tether/runner/codex_sdk_sidecar.py` — Codex sidecar adapter
- `agent/tether/api/runner_events.py` — RunnerEvents → SSE bridge
- `agent/tether/api/runner_registry.py` — Runner caching

## Tests

- `tests/test_runner_events.py` — All RunnerEvents callbacks
- `tests/test_claude_subprocess.py` — Claude subprocess adapter
- `tests/test_claude_sdk_worker.py` — Claude SDK worker (child process)
- `tests/test_runner_registry.py` — Registry caching
- `tests/test_sidecar_unavailable_error.py` — 503 error handling
