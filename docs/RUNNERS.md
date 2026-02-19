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
- Supports both CLI OAuth and API key auth (via `ANTHROPIC_API_KEY`)
- Adapter name: `claude_subprocess`

### Codex SDK Sidecar (`runner/codex_sdk_sidecar.py`)
- REST client to Codex sidecar process (port 8788)
- Session lifecycle via HTTP
- Event streaming
- Adapter name: `codex_sdk_sidecar`

### OpenCode SDK Sidecar (`runner/opencode_sdk_sidecar.py`)
- REST client to OpenCode sidecar process (port 8790)
- Session lifecycle via HTTP
- Event streaming
- Adapter name: `opencode`

### Auto-detection (`runner/__init__.py`)
`get_runner()` auto-selects adapter:
1. If `TETHER_DEFAULT_AGENT_ADAPTER` is set, use that
2. If Claude CLI OAuth or `ANTHROPIC_API_KEY` is available, use `claude_subprocess`
3. Raise error

## Runner Registry (`api/runner_registry.py`)

Caches runner instances. `get_runner_registry()` provides global singleton.
`get_api_runner(adapter_name)` returns the runner for a given adapter.

## Config

| Env Var | Description |
|---------|-------------|
| `TETHER_DEFAULT_AGENT_ADAPTER` | Force adapter: `claude_subprocess`, `codex_sdk_sidecar`, `opencode`, `litellm`, etc. |
| `ANTHROPIC_API_KEY` | API key for Claude (alternative to CLI OAuth) |
| `TETHER_AGENT_CLAUDE_MODEL` | Model override (default: claude-sonnet-4-20250514) |
| `TETHER_CODEX_SIDECAR_URL` | Sidecar URL (default: http://localhost:8788) |
| `TETHER_CODEX_SIDECAR_TOKEN` | Sidecar auth token |
| `TETHER_OPENCODE_SIDECAR_URL` | Sidecar URL (default: http://localhost:8790) |
| `TETHER_OPENCODE_SIDECAR_TOKEN` | Sidecar auth token |
| `TETHER_OPENCODE_SIDECAR_MANAGED` | Auto-start/stop sidecar from Tether (default: 1) |
| `TETHER_OPENCODE_SIDECAR_CMD` | Command for managed sidecar (default: `opencode serve`) |

## Key Files

- `agent/tether/runner/base.py` — Protocol definitions
- `agent/tether/runner/__init__.py` — Auto-detection + factory
- `agent/tether/runner/claude_subprocess.py` — Claude subprocess adapter (parent side)
- `agent/tether/runner/claude_sdk_worker.py` — Claude subprocess worker (child side)
- `agent/tether/runner/codex_sdk_sidecar.py` — Codex sidecar adapter
- `agent/tether/runner/opencode_sdk_sidecar.py` — OpenCode sidecar adapter
- `agent/tether/api/runner_events.py` — RunnerEvents → SSE bridge
- `agent/tether/api/runner_registry.py` — Runner caching

## Tests

- `tests/test_runner_events.py` — All RunnerEvents callbacks
- `tests/test_claude_subprocess.py` — Claude subprocess adapter
- `tests/test_claude_sdk_worker.py` — Claude SDK worker (child process)
- `tests/test_runner_registry.py` — Registry caching
- `tests/test_opencode_sidecar.py` — OpenCode sidecar adapter
- `tests/test_sidecar_unavailable_error.py` — 503 error handling
