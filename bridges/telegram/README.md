# Tether Telegram Bridge

A Telegram bot that bridges to the Tether agent, enabling mobile monitoring and control of AI coding sessions.

## Features

- Receive notifications when the agent needs input
- Send replies that are forwarded as session input
- View session status and switch between sessions
- Interrupt running sessions remotely

## Requirements

- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram chat ID

## Installation

```bash
cd bridges/telegram
pip install -e .
```

Or install dependencies directly:

```bash
pip install aiohttp python-telegram-bot
```

## Configuration

Set the following environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Your chat ID (use [@userinfobot](https://t.me/userinfobot) to find it) |
| `AGENT_URL` | No | Agent URL (default: `http://localhost:8787`) |
| `AGENT_TOKEN` | No | Agent auth token if configured |

## Usage

Start the bridge:

```bash
python -m bridges.telegram.main
```

Or if installed:

```bash
tether-telegram
```

## Commands

| Command | Description |
|---------|-------------|
| `/status` | List all sessions with their states |
| `/sessions` | Alias for /status |
| `/stop [id]` | Interrupt the active session or a specific session |
| `/switch <id>` | Switch the active session |
| `/help` | Show help message |

## Message Handling

- When a session transitions to `AWAITING_INPUT`, you receive a notification with the session name and recent output
- Any text message you send is forwarded as input to the active session
- The session that most recently requested input becomes the active session

## Session Selection

You can reference sessions by:
- Full session ID
- Session number from `/status` output (1-indexed)
- Partial session ID prefix

## Architecture

```
┌─────────────┐     SSE      ┌─────────────┐
│   Tether    │─────────────▶│  Telegram   │
│   Agent     │              │   Bridge    │
│             │◀─────────────│             │
└─────────────┘    HTTP      └──────┬──────┘
                                    │
                                    │ Telegram API
                                    ▼
                              ┌───────────┐
                              │ Telegram  │
                              │   User    │
                              └───────────┘
```

The bridge:
1. Subscribes to SSE events from active agent sessions
2. Sends Telegram messages when `input_required` events arrive
3. Routes user replies back to the agent via HTTP POST to `/api/sessions/{id}/input`
