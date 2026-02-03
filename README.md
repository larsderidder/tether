# <img src="tether_compact_logo.png" height="32"> Tether

[![CI](https://github.com/XIThing/tether/actions/workflows/ci.yml/badge.svg)](https://github.com/XIThing/tether/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Early_Development-orange.svg)]()

Your AI coding agents, in your pocket.

You start Claude Code or Codex on a task, walk away, and come back to find it stuck waiting for input for an hour. Tether fixes that.

Get notified in Telegram when your agent needs you. Respond from anywhere. Every agent session is a topic in a Telegram group - one place for all your agents.

## How it works

```
1. Start Tether on your machine (or VM)
2. Attach your Claude Code / Codex sessions
3. Each session becomes a topic in your Telegram group
4. Agent gets stuck → you get a notification
5. Reply in Telegram → agent continues
```

No port forwarding. No SSH. No web UI to expose. Just Telegram.

## Features

- **Telegram-first** - Every agent session is a topic in a group. Reply from anywhere.
- **Local-first** - Runs on your machine, your data stays yours
- **Multi-agent** - Claude Code and Codex supported, more coming
- **Web UI included** - Optional browser dashboard if you prefer it
- **No API keys required** - Uses Claude / Codex local OAuth by default

## Quick Start

```bash
git clone https://github.com/xithing/tether.git
cd tether
make install
make start
```

### Telegram setup

1. Create a bot via [@BotFather](https://t.me/BotFather)
2. Create a Telegram group, add your bot, enable topics
3. Set your bot token in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=your_token_here
   ```
4. Start Tether and attach your first agent session

Each session automatically creates a topic in your group.

### Web UI (optional)

Tether also includes a web dashboard at `http://localhost:8787`. Useful on desktop, but Telegram is the primary interface.

## Why Telegram?

| | SSH | Web UI | Telegram |
|---|---|---|---|
| Works from phone | Barely | Yes (if exposed) | Yes |
| Setup needed | Keys, terminal | Port forwarding / VPN | Bot token |
| Notifications | No | No | Yes, native |
| Reply inline | Awkward | Yes | Yes |
| Already installed | No | N/A | Probably |
| Multiple agents | Tabs/tmux | Dashboard | Topics |

## Adapters

Set `TETHER_AGENT_ADAPTER` in `.env`:

| Adapter | Description |
|---------|-------------|
| `claude_local` | Claude Code via local OAuth (default) |
| `claude_api` | Claude Code via API key |
| `codex_sdk_sidecar` | Codex via sidecar |
| `codex_cli` | Legacy Codex CLI |

## Configuration

Copy `.env.example` to `.env`. Key settings:

```bash
TELEGRAM_BOT_TOKEN=       # Your Telegram bot token
TETHER_AGENT_ADAPTER=     # Agent adapter (default: claude_local)
TETHER_AUTH_TOKEN=         # Optional: protect the web UI
```

## License

Apache 2.0
