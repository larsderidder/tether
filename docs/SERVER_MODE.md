# Server Mode Deployment Guide

How to run Tether on a Linux server so you can manage agent sessions remotely
from your phone or laptop via Telegram, Slack, Discord, or the CLI.

---

## Overview

In server mode Tether runs as a persistent daemon on a remote machine. Your
agents run there (with full disk access and API credentials), while you
supervise them from anywhere through messaging bridges or `tether` CLI flags.

```
[Your phone/laptop]            [Server]
  Telegram / Slack  ──────►  tether daemon  ──►  claude / codex / opencode
  tether -H mybox list ───►  port 8787
```

---

## Prerequisites

On the server:

- Linux (Ubuntu 22.04+ or Debian 12+ recommended)
- Python 3.11 or newer
- Git
- `pipx` (recommended) or `pip`
- At least one agent CLI installed: `claude`, `opencode`, or similar
- Tailscale (strongly recommended — see [Network access](#network-access))

---

## Installation

### From PyPI (recommended)

```bash
pipx install tether-ai
```

### From source

```bash
git clone https://github.com/yourusername/tether.git
cd tether/agent
pip install -e .
```

### Interactive setup

```bash
tether init
```

`tether init` generates a bearer token, detects installed agent adapters, and
writes `~/.config/tether/config.env`. Run it once before starting the server.

---

## Network access

### Option A: Tailscale (recommended)

Tailscale creates a private encrypted network between your devices. No port
forwarding or public exposure needed.

1. Install Tailscale on the server and on every client device:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```

2. Tether binds to `0.0.0.0:8787` by default, so it is reachable on the
   Tailscale interface once the service is running.

3. Find the server's Tailscale hostname:
   ```bash
   tailscale status
   ```

4. From your laptop or phone (also on Tailscale):
   ```bash
   tether -H myserver list
   # or set it once:
   # export TETHER_AGENT_HOST=myserver
   ```

### Option B: SSH tunnel (no extra software)

```bash
ssh -L 8787:localhost:8787 user@myserver
# Then on your laptop:
tether list   # connects to localhost:8787 through the tunnel
```

### Option C: Reverse proxy with TLS (public exposure)

Only use this if you need access from devices that cannot run Tailscale.
Place nginx or caddy in front, terminate TLS, and pass traffic to
`127.0.0.1:8787`. Ensure `TETHER_AGENT_TOKEN` is set to a strong random value.

---

## Systemd service

Create `/etc/systemd/system/tether.service`:

```ini
[Unit]
Description=Tether Agent Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tether
ExecStart=/home/tether/.local/bin/tether start
Restart=always
RestartSec=5

# Core settings (also in ~/.config/tether/config.env, but explicit here is safer)
Environment=TETHER_AGENT_TOKEN=<your-token>
Environment=TETHER_DEFAULT_AGENT_ADAPTER=claude_auto

# Agent API credentials
Environment=ANTHROPIC_API_KEY=<your-key>

# Messaging bridge (add only the ones you use)
Environment=TELEGRAM_BOT_TOKEN=<bot-token>
Environment=TELEGRAM_FORUM_GROUP_ID=<group-id>

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tether
sudo systemctl status tether
journalctl -fu tether   # follow logs
```

> **Tip:** Use a dedicated `tether` system account (`sudo useradd -m -s /bin/bash tether`)
> so agent processes run with limited permissions.

---

## SSH keys for git cloning

When sessions clone repositories (via `tether new --clone`), the server's git
client needs read access to those repos.

### GitHub / GitLab deploy keys

1. Generate a key on the server:
   ```bash
   ssh-keygen -t ed25519 -C "tether@myserver" -f ~/.ssh/tether_git
   ```

2. Add the public key (`~/.ssh/tether_git.pub`) as a **Deploy Key** on the
   relevant GitHub/GitLab repositories (read-only is enough for cloning).

3. Configure `~/.ssh/config` (as the `tether` user):
   ```
   Host github.com
       IdentityFile ~/.ssh/tether_git
       StrictHostKeyChecking accept-new
   ```

4. Test:
   ```bash
   ssh -T git@github.com
   ```

### Agent git operations

Agent processes (Claude Code, etc.) also run git commands inside the cloned
workspace. They inherit the same `~/.ssh` config, so the same deploy key
covers push/pull during the session. For push access, ensure the deploy key
has write permission or use a personal access token via HTTPS.

---

## Agent authentication

| Adapter | Credential | Where to set |
| --- | --- | --- |
| `claude_auto` / `claude_subprocess` | `ANTHROPIC_API_KEY` | systemd `Environment=` or `config.env` |
| `opencode` | Provider API key (e.g. `OPENAI_API_KEY`) | same |
| `pi` (Claude Code via pi) | `ANTHROPIC_API_KEY` + pi installed | same |
| `codex_sdk_sidecar` | `OPENAI_API_KEY` | same |

API-key based auth is simplest for servers — no browser OAuth required.

---

## CLI access from your laptop

Once the server is running, connect from any machine with `tether` installed:

```bash
# One-off
tether --host myserver --token <token> list

# Persistent: named server profile in ~/.config/tether/servers.yaml
# servers:
#   work:
#     host: myserver
#     port: 8787
#     token: <token>
# default: work

tether list                        # uses default server
tether -S work new --clone git@github.com:owner/repo.git --auto-branch
tether -S work git status <id>
```

---

## Data persistence

| Path | Contents |
| --- | --- |
| `~/.local/share/tether/sessions.db` | SQLite session store |
| `~/.local/share/tether/sessions/*/events.jsonl` | Per-session event logs |
| `~/.local/share/tether/workspaces/` | Cloned repo workspaces |
| `~/.config/tether/config.env` | Server configuration |

**Backup:** `tar czf tether-backup.tar.gz ~/.local/share/tether/`

The data directory can be overridden with `TETHER_AGENT_DATA_DIR`.

---

## Security considerations

- **Token:** set `TETHER_AGENT_TOKEN` to a strong random value
  (`openssl rand -hex 32`). All API requests require this token.
- **Network:** Tailscale keeps the service off the public internet. If you
  must expose it publicly, use a TLS-terminating reverse proxy.
- **Process isolation:** agent processes run as the `tether` OS user. There is
  no container isolation between sessions. Treat the server as a trusted
  development machine, not a multi-tenant system.
- **API keys:** store credentials in the systemd unit file or
  `~/.config/tether/config.env`, not in session-visible environment variables
  that agents could read.

---

## Firewall

If using Tailscale you typically do not need to open port 8787 externally.
To allow LAN access only:

```bash
# ufw
sudo ufw allow from 100.0.0.0/8 to any port 8787   # Tailscale range

# firewalld
sudo firewall-cmd --add-rich-rule='rule family=ipv4 source address=100.0.0.0/8 port port=8787 protocol=tcp accept' --permanent
sudo firewall-cmd --reload
```

---

## Upgrading

```bash
pipx upgrade tether-ai
sudo systemctl restart tether
```

The SQLite database schema is managed with Alembic migrations that run
automatically on startup. No manual migration steps are needed.

---

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `Connection refused` from CLI | Is `tether` running? `systemctl status tether` |
| `401 Unauthorized` | `TETHER_AGENT_TOKEN` mismatch between server and CLI |
| Agent sessions don't start | Agent binary installed and on PATH? Check `journalctl -fu tether` |
| Git clone fails | SSH key configured? `ssh -T git@github.com` as the tether user |
| Sessions lost after restart | Check data dir permissions; confirm `TETHER_AGENT_DATA_DIR` unchanged |
