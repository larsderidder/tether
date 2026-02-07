# AI Agent Instructions

This file is the canonical instructions entrypoint for AI agents working in this repository.

## First Step

**Ask the user what they want to do:**
- Set up and run Tether for personal use
- Develop on the Tether repository

If they want to **develop**, direct them to CONTRIBUTING.md and get out of their way.

If they want to **set up Tether**, continue with the instructions below.

---

## Project Overview

Tether is a **local-first control plane for supervising AI work**. It lets you start, monitor,
and guide AI-driven tasks from anywhere (especially your phone), without giving up control,
visibility, or ownership of your environment.

Key principles:
- **Local-first**: Runs on your machine, no cloud dependency
- **Human-in-the-loop**: AI is not autonomous; human remains in control
- **Observable over magical**: Visible logs, explicit diffs, simple primitives

## Prerequisites

- Python 3.10+
- Node.js 20+
- Git

## Setup

```bash
# Clone the repository
git clone https://github.com/larsderidder/tether.git
cd tether

# Install dependencies
make install

# Copy the example config
cp .env.example .env
```

### Configure .env

1. **Ask the user** which AI model they want to use: **Claude** (default) or **Codex**

2. (Recommended) Generate a secure token and update `.env`:
   - Set `TETHER_AGENT_TOKEN` to a random value (e.g., `openssl rand -hex 16`)
   - If user chose Codex, set `TETHER_AGENT_ADAPTER=codex_sdk_sidecar`
   - **Show the user the token** - it’s needed for the web UI, API requests, and MCP server calls when auth is enabled

3. Start the agent:
   ```bash
   # For Claude (default)
   make start

   # For Codex
   make start-codex
   ```

## Verify

Run the verify script (checks both agent API and UI):

```bash
make verify
```

Or manually:
1. Open http://localhost:8787 in a browser
2. The Tether UI should load

## Phone Access

To access Tether from a phone on the same network:

1. Find the computer's IP address
2. Open the firewall port:

   **Linux (firewalld):**
   ```bash
   sudo firewall-cmd --add-port=8787/tcp --permanent && sudo firewall-cmd --reload
   ```

   **Linux (ufw):**
   ```bash
   sudo ufw allow 8787/tcp
   ```

   **macOS:**
   System Settings > Network > Firewall > Options > Allow incoming connections

3. Open `http://<computer-ip>:8787` on the phone

## Docker Alternative

If the user has trouble with Python/Node dependencies, Docker can be used as a backup:

```bash
make docker-start
```

**Important:** The Docker setup requires mapping host directories for the agent to access code repositories. Add volume mounts to `docker-compose.yml`:

```yaml
services:
  agent:
    volumes:
      - /home/username:/home/username
```

The native setup (`make start`) is recommended as it has direct file system access.

## Troubleshooting

If `make install` fails:
- Check Python version: `python --version` (needs 3.10+)
- Check Node version: `node --version` (needs 20+)

If `make start` fails:
- Check if port 8787 is in use: `lsof -i :8787`
- Check the error output for missing dependencies

## Next Steps

Once running, the user can:
- Access from phone: open `http://<computer-ip>:8787` on the same network
- See README.md for configuration options
- See CONTRIBUTING.md for development setup

---

## Git Commit Policy

**Follow these rules exactly:**

- Use a **single-line commit message** (no multi-line descriptions)
- Keep messages concise and descriptive
- Use **sentence case** (e.g., "Add feature" not "add feature")

Examples:
```
Add settings module tests
Refactor sidecar into modular structure with centralized settings
Fix token validation in auth middleware
```

## Code Standards

- **Python**: Use Black formatter (`cd agent && python -m black .`)
- **Type hints**: Keep annotations up to date; prefer modern syntax (`list`, `dict`, `| None`)
- **Logging**: Use structured logging (structlog) with request identifiers
- **Docstrings**: Use for non-trivial logic; keep concise

See `background/CODE_STANDARDS.md` for full details.

## Architecture

Components:
- **Agent (Python/FastAPI)**: HTTP API, SSE streaming, static UI hosting
- **UI (Vue 3)**: Mobile-first interface for session monitoring
- **Runner**: Execution adapter (Codex CLI, Claude, etc.)

See `background/ARCHITECTURE.md` for details.

## Key Background Documents

For more context, see the `background/` directory, especially:

| Document | Purpose |
| --- | --- |
| `background/GOAL.md` | Project philosophy and success criteria |
| `background/ARCHITECTURE.md` | System components and data flow |
| `background/PROTOCOL.md` | HTTP API and SSE protocol specification |
| `background/RUNNER_SPEC.md` | Runner contract and event semantics |
| `background/CODE_STANDARDS.md` | Formatting, typing, logging standards |
| `background/ROADMAP.md` | Development phases and priorities |

## Design Principles

- Local-first
- Explicit > implicit
- Observable > automated
- Simple > clever
- Human remains in control
