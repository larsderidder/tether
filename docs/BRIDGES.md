# Bridges (Messaging Platforms)

Bridges connect Tether's session events to messaging platforms — Telegram, Slack, Discord. Users interact with agents through chat threads.

## Architecture

```
Store events ──> BridgeSubscriber ──> BridgeManager ──> Platform Bridge
                   (per session)       (registry)         (Telegram/Slack/Discord)
```

### Pattern: Strategy + Registry
- `BridgeInterface` (ABC) — shared base with abstract methods + shared helpers
- `BridgeManager` — singleton registry mapping platform names to bridge instances
- `BridgeSubscriber` — background task per session consuming events from store queue

## BridgeInterface (`bridges/base.py`)

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

## BridgeSubscriber (`bridges/subscriber.py`)

Routes store events to bridge methods:
- `output` with `final=True` → `on_output()` (skips non-final, history, empty)
- `output_final` → skipped (accumulated blob, bridges get individual finals)
- `permission_request` → builds `ApprovalRequest`, calls `on_approval_request()`
- `session_state` RUNNING → `on_typing()`
- `session_state` ERROR → `on_status_change("error")`
- `error` → `on_status_change("error", metadata)`

## Platform Implementations

### Telegram (`bridges/telegram/`)
- **bot.py** — Full-featured: forum topics, inline keyboards, HTML formatting, replay, `/attach`, `/list`, `/stop`, `/usage`, `/help`
- **state.py** — Persists session↔topic mappings to JSON, `remove_session()` for cleanup
- **formatting.py** — `markdown_to_telegram_html()`, `strip_tool_markers()`, `_markdown_table_to_pre()`, `chunk_message()`
- Approval UI: inline keyboard with Allow, Deny, Allow {tool} (30m), Allow All (30m), Show All
- Auto-approve sends `✅ <b>Tool</b> — auto-approved (reason)` notification

### Slack (`bridges/slack/`)
- **bot.py** — Thread-based: `!attach`, `!list`, `!stop`, `!usage`, `!help`, `!status`
- Socket mode for real-time events (requires `SLACK_APP_TOKEN`)
- Text-based approval: reply `allow`, `deny`, `allow all`, `allow {tool}`
- Auto-approve sends `✅ *Tool* — auto-approved (reason)` notification

### Discord (`bridges/discord/`)
- **bot.py** — Thread-based: same `!` commands as Slack
- discord.py client with message_content intent
- Text-based approval: same as Slack
- Auto-approve sends `✅ **Tool** — auto-approved (reason)` notification

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

Bridges auto-initialize in `main.py` lifespan if tokens are configured.

## Key Files

- `agent/tether/bridges/base.py` — Interface + shared logic
- `agent/tether/bridges/manager.py` — BridgeManager singleton
- `agent/tether/bridges/subscriber.py` — Event consumer/router
- `agent/tether/bridges/telegram/` — Telegram implementation
- `agent/tether/bridges/slack/` — Slack implementation
- `agent/tether/bridges/discord/` — Discord implementation

## Tests

- `tests/test_bridge_base.py` — Auto-approve, pagination, usage formatting, cleanup
- `tests/test_subscriber.py` — Event routing, lifecycle, error resilience
- `tests/test_telegram_bridge.py` — Interface, output, approvals, topics, state
- `tests/test_slack_bridge.py` — Interface, output, approvals, threads
- `tests/test_discord_bridge.py` — Interface, output, approvals, threads
- `tests/test_formatting.py` — HTML conversion, tables, tool markers, chunking
- `tests/test_external_agent_api.py` — Bridge manager routing
