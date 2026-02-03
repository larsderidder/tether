# Setup Instructions for AI Agents

## First Step

**Ask the user what they want to do:**
- Set up and run Tether for personal use
- Develop on the Tether repository

If they want to **develop**, direct them to CONTRIBUTING.md for the development setup.

If they want to **set up Tether**, continue with the instructions below.

---

## Prerequisites

- Python 3.10+
- Node.js 20+
- Git

## Setup

```bash
# Clone the repository
git clone https://github.com/XIThing/tether.git
cd tether

# Install dependencies
make install

# Copy the example config
cp .env.example .env
```

### Configure .env

1. **Ask the user** which AI model they want to use: **Claude** (default) or **Codex**

2. Generate a secure token and update `.env`:
   - Set `TETHER_AGENT_TOKEN` to a random value (e.g., `openssl rand -hex 16`)
   - If user chose Codex, set `TETHER_AGENT_ADAPTER=codex_sdk_sidecar`
   - **Show the user the token** - they need it to log in to the web UI

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
