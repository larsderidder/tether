.PHONY: start start-codex stop build-ui install verify \
        docker-start docker-start-codex docker-stop docker-logs docker-status docker-build docker-clean \
        dev dev-ui dev-stop test

# =============================================================================
# Native mode (recommended)
# =============================================================================

# Install dependencies (run once)
install:
	cd agent && pip install -e ".[dev]"
	cd ui && npm ci

# Build UI for production
build-ui:
	cd ui && npm run build
	rm -rf agent/tether/static_ui
	cp -r ui/dist agent/tether/static_ui

# Start agent natively (Claude auto-detect works out of the box)
start: build-ui
	cd agent && python -m tether.main

# Start agent + Codex sidecar (sidecar runs in Docker)
start-codex: build-ui
	docker compose --profile codex up -d --build codex-sidecar
	cd agent && TETHER_AGENT_ADAPTER=codex_sdk_sidecar python -m tether.main

# Stop sidecar container
stop:
	docker compose stop codex-sidecar 2>/dev/null || true

# =============================================================================
# Development mode
# =============================================================================

# Run UI dev server (hot reload) - run agent separately
dev-ui:
	cd ui && npm run dev

# Run Codex sidecar in Docker for development
dev:
	docker compose -f docker-compose.dev.yml up

dev-stop:
	docker compose -f docker-compose.dev.yml down

# Run tests
test:
	cd agent && pytest

# Verify setup (agent must be running)
verify:
	./scripts/verify.sh

# =============================================================================
# Docker mode (legacy - for users who prefer Docker with volume mounts)
# =============================================================================

docker-start:
	docker compose up -d agent

docker-start-codex:
	docker compose --profile codex up -d codex-sidecar

docker-stop:
	docker compose --profile codex down

docker-logs:
	docker compose logs -f

docker-status:
	docker compose ps -a

docker-build:
	docker compose build

docker-clean:
	docker compose --profile codex down -v
