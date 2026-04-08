# 2026-04-08 Stability and component extraction analysis

## Scope

Focused stability pass on backend runtime paths and API lifecycle handling.

Code fixes validated by full test suite.

## Stability fixes applied

1. Bridge thread creation compatibility fallback
- Added `create_or_reuse_thread` in `agent/tether/bridges/glue.py`.
- Handles mixed bridge versions that do not yet support `existing_thread_id`.
- Retries thread creation without `existing_thread_id` only when this specific signature mismatch is detected.

2. External attach flow crash fix
- Removed local shadowing import in `agent/tether/api/external_sessions.py` that could trigger `UnboundLocalError` for `raise_http_error`.

3. Idle timeout timestamp correctness
- Replaced local-time parsing in `agent/tether/maintenance.py` (`time.mktime`) with UTC-aware parsing via `datetime(..., tzinfo=timezone.utc).timestamp()`.
- Prevents timezone and DST-dependent idle timeout behavior.

4. Regression tests
- Added `agent/tests/test_maintenance.py` to assert UTC parsing correctness and invalid timestamp handling.
- Updated bridge test doubles to accept optional `existing_thread_id` across several test files.

## Verification

- Targeted tests: passed.
- Full suite: `953 passed`.

## Component extraction opportunities for stability

### 1) Session lifecycle orchestration
Current pain:
- `agent/tether/api/sessions.py` is large (965 lines).
- `create_session`, `start_session`, `send_input`, `interrupt_session` each mix validation, state transitions, runner orchestration, event emission, and error mapping.

Extraction target:
- New module `tether/session_service.py` with explicit use cases:
  - `create_session(...)`
  - `start_turn(...)`
  - `send_input(...)`
  - `interrupt(...)`
- Keep API layer as thin request and response translation only.

Stability gain:
- One implementation of transition and rollback rules.
- Easier to unit test failure permutations without HTTP harness.
- Lower chance of lock ordering regressions.

### 2) Runner error normalization
Current pain:
- Similar error mapping logic is repeated in multiple endpoints.

Extraction target:
- `tether/runner/errors.py`
  - `map_runner_exception(adapter, exc) -> ApiError`
- Reuse in start, input, interrupt, and external attach resume paths.

Stability gain:
- Consistent status codes and messages.
- Reduced drift when adding new adapters.

### 3) Bridge binding and replay policy
Current pain:
- Bridge binding, thread creation, reuse behavior, and replay decisions are spread across `api/sessions.py`, `api/external_sessions.py`, and `bridges/glue.py`.

Extraction target:
- `tether/bridges/binding_service.py`
  - `bind_session_to_platform(session, platform, replay_source=...)`
  - centralizes create-or-reuse semantics and replay policy.

Stability gain:
- Single point for compatibility fallback and replay decisions.
- Fewer chances for inconsistent thread behavior across APIs.

### 4) CLI command routing and connection resolution
Current pain:
- `agent/tether/cli.py` is large (1125 lines), with parser setup and command orchestration mixed.

Extraction target:
- `tether/cli/parser.py` (arg parser construction)
- `tether/cli/dispatch.py` (command dispatch table)
- `tether/cli/connection.py` (`apply_connection_args`, precedence logic)

Stability gain:
- Parsing tests can target parser in isolation.
- Less brittle monkeypatching in command tests.
- Lower coupling between parsing and side effects.

### 5) Workspace and git safety boundaries
Current pain:
- `workspace.py` and `git_ops.py` contain many exception branches and mixed responsibilities.

Extraction target:
- `tether/workspace/repo_registry_service.py`
- `tether/workspace/worktree_service.py`
- `tether/workspace/prune_service.py`
- Shared typed result objects for operations (`success`, `error_code`, `details`).

Stability gain:
- Less broad `except Exception` handling in call sites.
- Better observability and deterministic cleanup paths.

### 6) Background tasks framework
Current pain:
- `maintenance_loop` and sidecar loops each implement custom infinite loop and recovery behavior.

Extraction target:
- `tether/background/loop_runner.py`
  - reusable supervised loop with jittered backoff and structured error labels.

Stability gain:
- Uniform crash containment for long-running loops.
- Better restart and shutdown semantics.

## Prioritized extraction plan

1. Session lifecycle service (highest risk reduction)
2. Bridge binding service
3. Runner error normalization
4. CLI parser and dispatch split
5. Workspace and git service split
6. Shared background loop runner

## Guardrails to add during extraction

- Require contract tests for each extracted service before endpoint rewiring.
- Keep lock and transition rules in one place and assert with state machine tests.
- Add golden tests for API error payloads per adapter and failure mode.
- For bridge binding, add tests for both new and old bridge signatures.
- Track extraction progress with per-service coverage thresholds.
