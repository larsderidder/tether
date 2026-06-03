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
| `on_output(session_id, stream, text, kind, is_final, bridge_segments)` | Agent output text plus optional structured bridge segments |
| `on_header(session_id, title, model, provider, ...)` | Session metadata header |
| `on_error(session_id, code, message)` | Agent error |
| `on_exit(session_id, exit_code)` | Process terminated |
| `on_awaiting_input(session_id)` | Agent needs user input |
| `on_metadata(session_id, key, value, raw)` | Tokens, cost, model info |
| `on_heartbeat(session_id, elapsed_s, done)` | Liveness signal |
| `on_permission_request(session_id, request_id, ...)` | Permission request from agent |
| `on_permission_resolved(session_id, request_id, ...)` | Permission resolved |

Implemented by `ApiRunnerEvents` in `api/runner_events.py` which bridges to SSE events. `bridge_segments` is optional and lets runners send typed bridge output (`assistant`, `thinking`, `tool_call`, `tool_output`, `tool_result`, `tool_error`, `status`) while preserving plain text for the web UI and older consumers.

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

### Runbook (`runner/runbook.py`)
- Executes local YAML-defined runbooks as subprocess steps
- Adapter name: `runbook`
- Loads runbooks from `.tether/runbooks/*.yaml` in the session directory and `~/.config/tether/runbooks/*.yaml`
- Saves each turn to `runbook-runs/<session>/<runbook>-<id>/` under the Tether data directory
- Writes `manifest.json`, input images, `output.md`, `output.json`, and `run.log`
- Uses argv-list commands only; `shell: true` is rejected

Example runbook:

```yaml
name: pokemon-photo-triage
description: Triage Pokémon card shop photos
timeout_seconds: 180
steps:
  - name: triage
    run:
      cwd: /home/lars/workspace/akihabara-figure-shopping
      command:
        - python3
        - collectible_hunt.py
        - cards
        - photo-triage
        - --manifest
        - "{manifest}"
        - --markdown
        - "{output_md}"
```

Start one from Telegram with `/new runbook /path/to/project`, then send photos in the created topic. If the session can see more than one runbook, use the runbook name as the first word of the message.

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
| `TETHER_DEFAULT_AGENT_ADAPTER` | Force adapter: `claude_subprocess`, `codex_sdk_sidecar`, `opencode`, `litellm`, `runbook`, etc. |
| `ANTHROPIC_API_KEY` | API key for Claude (alternative to CLI OAuth) |
| `TETHER_AGENT_CLAUDE_MODEL` | Model override (default: claude-sonnet-4-20250514) |
| `TETHER_CODEX_SIDECAR_URL` | Sidecar URL (default: http://localhost:8788) |
| `TETHER_CODEX_SIDECAR_TOKEN` | Sidecar auth token |
| `TETHER_OPENCODE_SIDECAR_URL` | Sidecar URL (default: http://localhost:8790) |
| `TETHER_OPENCODE_SIDECAR_TOKEN` | Sidecar auth token |
| `TETHER_OPENCODE_SIDECAR_MANAGED` | Auto-start/stop sidecar from Tether (default: 1) |
| `TETHER_OPENCODE_SIDECAR_CMD` | Command for managed sidecar (default: `opencode serve`) |
| `TETHER_PI_RESUME_MAX_SESSION_FILE_BYTES` | Max pi session file size Tether will resume (default: 157286400, range: 10MB-1GB) |
| `TETHER_PI_TOOL_OUTPUT_MAX_CHARS` | Max characters kept from pi tool output in Tether events (default: 1200, range: 200-20000) |
| `TETHER_PI_TOOL_OUTPUT_MAX_LINES` | Max lines kept from pi tool output in Tether events (default: 80, range: 5-1000) |

## Pi RPC Compaction

The `pi_rpc` adapter can request manual pi compaction through `POST /api/sessions/{id}/compact`. Tether sends the RPC `compact` command to pi and renders `compaction_start` / `compaction_end` status events to bridges. The bridge command is `!compact [instructions]` on Slack and Discord, `/compact [instructions]` on Telegram.

Compaction is lossy for active model context but does not shrink pi's JSONL history file. Large session files may still be too big to resume with a provider even after compaction.

## Key Files

- `agent/tether/runner/base.py` — Protocol definitions
- `agent/tether/runner/__init__.py` — Auto-detection + factory
- `agent/tether/runner/claude_subprocess.py` — Claude subprocess adapter (parent side)
- `agent/tether/runner/claude_sdk_worker.py` — Claude subprocess worker (child side)
- `agent/tether/runner/codex_sdk_sidecar.py` — Codex sidecar adapter
- `agent/tether/runner/opencode_sdk_sidecar.py` — OpenCode sidecar adapter
- `agent/tether/runner/runbook.py` — Local runbook adapter
- `agent/tether/runbooks.py` — Runbook config loader
- `agent/tether/api/runner_events.py` — RunnerEvents → SSE bridge
- `agent/tether/api/runner_registry.py` — Runner caching

## Server Mode Notes

Runners work identically in server mode (running on a remote machine). A few
things to keep in mind:

- **API credentials** — Set `ANTHROPIC_API_KEY` (or equivalent) in the systemd
  unit file or `~/.config/tether/config.env` on the server. Runners inherit
  the daemon's environment.
- **SSH keys for git** — Runners that make git commits or pushes inside a
  cloned workspace use the server's `~/.ssh` config. Ensure a deploy key (or
  personal access token via HTTPS) is configured before starting sessions that
  need to push. See [Server Mode > SSH keys](SERVER_MODE.md#ssh-keys-for-git-cloning).
- **Auto-checkpoint** — When `TETHER_GIT_AUTO_CHECKPOINT=true`, `on_awaiting_input`
  triggers a git commit in the workspace after each turn. The runner itself is
  not involved; this is handled by `api/runner_events.py`.

## Tests

- `tests/test_runner_events.py` — All RunnerEvents callbacks
- `tests/test_claude_subprocess.py` — Claude subprocess adapter
- `tests/test_claude_sdk_worker.py` — Claude SDK worker (child process)
- `tests/test_runner_registry.py` — Registry caching
- `tests/test_opencode_sidecar.py` — OpenCode sidecar adapter
- `tests/test_runbook_runner.py` — Runbook config, image manifest, and output handling
- `tests/test_sidecar_unavailable_error.py` — 503 error handling
