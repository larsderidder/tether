# Contributing

## Requirements

- Python 3.10+
- Node.js 20+
- Docker (optional, for Codex sidecar)

## Development Setup

### Quick Start

```bash
# Install all dependencies
make install

# Terminal 1: Run agent
cd agent && python -m tether.main

# Terminal 2: Run UI with hot reload
make dev-ui
```

Open http://localhost:5173 (Vite dev server proxies API to agent).

### Agent (Python)

```bash
cd agent
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

# Run agent
python -m tether.main

# Run tests
pytest
```

### UI (Vue)

```bash
cd ui
npm install
npm run dev      # Dev server with hot reload
npm run build    # Production build
npm test         # Run tests
```

### With Codex Sidecar

```bash
# Start sidecar in Docker
docker compose --profile codex up -d codex-sidecar

# Run agent with TETHER_AGENT_ADAPTER=codex_sdk_sidecar
TETHER_AGENT_ADAPTER=codex_sdk_sidecar python -m tether.main
```

## Commands

```bash
make install      # Install Python and Node dependencies
make start        # Build UI and run agent
make start-codex  # Build UI, start sidecar, run agent
make stop         # Stop sidecar container
make dev-ui       # Run UI dev server (hot reload)
make dev          # Run Codex sidecar in Docker (watch mode)
make dev-stop     # Stop dev containers
make test         # Run agent tests
```

## Code Style

See [background/CODE_STANDARDS.md](background/CODE_STANDARDS.md) for style guidelines.

- Python: Format with Black (`cd agent && python -m black .`)
- Commits: Single-line, sentence case, no AI attribution
