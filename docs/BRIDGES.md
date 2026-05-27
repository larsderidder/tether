# Bridges (Messaging Platforms)

Bridges connect Tether's session events to messaging platforms — Telegram, Slack, Discord. Users interact with agents through chat threads.

See [Architecture](ARCHITECTURE.md) for visual diagrams of where bridges fit in the overall system.

## Architecture

```
Store events ──> BridgeSubscriber ──> BridgeManager ──> Platform Bridge
                   (per session)       (registry)         (Telegram/Slack/Discord)
```

Bridges are one of the event consumption paths from the store subscriber queue (the other is the SSE stream for external API clients). Bridges filter events server-side (only final output, permission requests, state changes) and render for text-based messaging platforms. See [Session Engine > Event Distribution](SESSION_ENGINE.md#event-distribution) for the full picture.

### Pattern: Strategy + Registry
- `BridgeInterface` (ABC) — shared base with abstract methods + shared helpers
- `BridgeManager` — singleton registry mapping platform names to bridge instances
- `BridgeSubscriber` — background task per session consuming events from store queue

## BridgeInterface (`agent/tether/bridges/base.py`)

Abstract methods every bridge must implement:
- `on_output(session_id, text)` — send agent output
- `on_approval_request(session_id, request)` — send approval prompt
- `on_status_change(session_id, status)` — send status update
- `create_thread(session_id, name)` — create platform thread

Optional overrides:
- `on_typing(session_id)` — show typing indicator (default no-op)
- `on_session_removed(session_id)` — cleanup on delete (default cleans timers)

Shared helpers (in base class):
- `check_auto_approve()` / `set_allow_all()` / `set_allow_tool()` — auto-approve timers (30m)
- `_auto_approve()` — silently approve via internal API + send notification
- `_format_external_page()` — paginated external session listing
- `_set_external_view()` — filter/search cached external sessions
- `_fetch_usage()` / `_format_usage_text()` — token usage display
- `_api_url()` / `_api_headers()` — internal API helpers

## BridgeSubscriber (`agent/tether/bridges/subscriber.py`)

Routes store events to bridge methods:
- `output` with `final=True` → `on_output()` (skips non-final, history, empty)
- `output_final` → skipped (accumulated blob, bridges get individual finals)
- `permission_request` → builds `ApprovalRequest`, calls `on_approval_request()`
- `session_state` RUNNING → `on_typing()`
- `session_state` ERROR → `on_status_change("error")`
- `error` → `on_status_change("error", metadata)`

## Platform Implementations

### Telegram (`agent/tether/bridges/telegram/`)
- **bot.py** — Full-featured: forum topics, inline keyboards, HTML formatting, replay, `/attach`, `/list`, `/stop`, `/usage`, `/help`
- **state.py** — Persists session↔topic mappings to JSON, `remove_session()` for cleanup
- **formatting.py** — `markdown_to_telegram_html()`, `strip_tool_markers()`, `_markdown_table_to_pre()`, `chunk_message()`
- Approval UI: inline keyboard with Allow, Deny, Allow {tool} (30m), Allow All (30m), Show All
- Auto-approve sends `✅ <b>Tool</b> — auto-approved (reason)` notification

### Slack (`agent/tether/bridges/slack/`)
- **bot.py** — Thread-based: `!attach`, `!list`, `!stop`, `!usage`, `!help`, `!status`
- Git commands (inside a session thread): `!git`, `!commit <msg>`, `!push`, `!pr <title> [--draft]`
- Socket mode for real-time events (requires `SLACK_APP_TOKEN`)
- Text-based approval: reply `allow`, `deny`, `allow all`, `allow {tool}`
- Auto-approve sends `✅ *Tool* — auto-approved (reason)` notification
- Optional reaction shortcut: react with `✅` to a top-level control-channel message whose first line starts with `!new ...` and whose remaining body is the initial prompt
- Optional plain-message reaction mode: when `TETHER_BRIDGE_REACTION_NEW_SESSION_ALLOW_PLAIN_MESSAGES=1`, react with `✅` to any top-level non-command control-channel message to use that full message as the initial prompt in the Tether server's current working directory

### Discord (`agent/tether/bridges/discord/`)
- **bot.py** — Thread-based: same `!` commands as Slack
- Git commands (inside a session thread): `!git`, `!commit <msg>`, `!push`, `!pr <title> [--draft]`
- discord.py client with message_content intent
- Text-based approval: same as Slack
- Auto-approve sends `✅ **Tool** — auto-approved (reason)` notification
- Optional reaction shortcut: react with `✅` to a top-level control-channel message whose first line starts with `!new ...` and whose remaining body is the initial prompt
- Optional plain-message reaction mode: when `TETHER_BRIDGE_REACTION_NEW_SESSION_ALLOW_PLAIN_MESSAGES=1`, react with `✅` to any top-level non-command control-channel message to use that full message as the initial prompt in the Tether server's current working directory
- Optional pairing/allowlist: when enabled, only authorized Discord user IDs can run commands or send input
- Optional no-ID setup: if `DISCORD_CHANNEL_ID` is unset, run `!setup <code>` in the desired channel to configure it

## Bridge Git Commands

Available inside a session thread on Slack and Discord (and via Telegram `/commit`, `/push`, `/pr`):

| Command | Action |
|---------|--------|
| `!git` | Show git status: branch, ahead/behind, changed files, last commit |
| `!commit <message>` | Stage all changes and commit |
| `!push` | Push current branch to origin |
| `!pr <title> [--draft]` | Create a pull/merge request via `gh` or `glab` |

These call the git API endpoints on the Tether server. The session workspace
must be a git repository (i.e. the session was created with `--clone`). Git
write commands are blocked while the session is `RUNNING`.

Forge detection is automatic: `github.com` URLs use `gh`, GitLab URLs use `glab`.

## Auto-Approve System

Stored in base class as in-memory dicts:
- `_allow_all_until[session_id] → expiry_timestamp` — approve everything for 30m
- `_allow_tool_until[session_id][tool_name] → expiry_timestamp` — approve specific tool for 30m
- `check_auto_approve()` checks both (Allow All takes precedence), returns reason string or None
- `on_session_removed()` cleans up both dicts

## Config

| Env Var | Description |
|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_FORUM_GROUP_ID` | Telegram supergroup ID (forum mode) |
| `SLACK_BOT_TOKEN` | Slack bot token (xoxb-) |
| `SLACK_APP_TOKEN` | Slack app token for socket mode |
| `SLACK_CHANNEL_ID` | Slack channel ID |
| `DISCORD_BOT_TOKEN` | Discord bot token |
| `DISCORD_CHANNEL_ID` | Discord channel ID (int) |
| `DISCORD_REQUIRE_PAIRING` | Require pairing before using the Discord bot (0/1) |
| `DISCORD_PAIRING_CODE` | Optional fixed pairing code (if unset and pairing is required, one is generated and logged) |
| `DISCORD_ALLOWED_USER_IDS` | Comma-separated Discord user IDs that are always authorized |
| `TETHER_BRIDGE_REACTION_NEW_SESSION_ENABLED` | Enable the `!new` plus checkmark reaction shortcut in Slack and Discord (default `1`) |
| `TETHER_BRIDGE_REACTION_NEW_SESSION_EMOJI` | Emoji or reaction name used for the new-session shortcut (default `✅`) |
| `TETHER_BRIDGE_REACTION_NEW_SESSION_ALLOW_PLAIN_MESSAGES` | Allow reacted top-level non-command control-channel messages to use their full text as the prompt in the Tether server's current working directory (default `0`) |

Bridges auto-initialize in `main.py` lifespan if tokens are configured.

## Key Files

- `agent/tether/bridges/base.py` — Interface + shared logic
- `agent/tether/bridges/manager.py` — BridgeManager singleton
- `agent/tether/bridges/subscriber.py` — Event consumer/router
- `agent/tether/bridges/telegram/` — Telegram implementation
- `agent/tether/bridges/slack/` — Slack implementation
- `agent/tether/bridges/discord/` — Discord implementation

## Tests

- `agent/tests/test_bridge_base.py` — Auto-approve, pagination, usage formatting, cleanup
- `agent/tests/test_subscriber.py` — Event routing, lifecycle, error resilience
- `agent/tests/test_telegram_bridge.py` — Interface, output, approvals, topics, state
- `agent/tests/test_slack_bridge.py` — Interface, output, approvals, threads
- `agent/tests/test_discord_bridge.py` — Interface, output, approvals, threads
- `agent/tests/test_formatting.py` — HTML conversion, tables, tool markers, chunking
- `agent/tests/test_external_agent_api.py` — Bridge manager routing
