.PHONY: start start-codex install install-codex build-ui build-sidecars dev-ui verify test

# =============================================================================
# Native mode (recommended)
# =============================================================================

# Install dependencies (run once)
install:
	cd agent && pip install -e ".[dev]"

install-sidecars:
	cd codex-src/sdk/typescript && npm install --ignore-scripts
	npm install --workspaces

install-codex: install-sidecars

install-ui:
	cd ui && npm ci

# Build UI for production
build-ui:
	cd ui && npm run build
	rm -rf agent/tether/static_ui
	cp -r ui/dist agent/tether/static_ui

# Build TypeScript sidecars into bundled JS for the Python package
build-sidecars:
	./scripts/build-sidecars.sh

# Start agent natively (Claude auto-detect works out of the box)
start: build-ui build-sidecars
	cd agent && python -m tether.main

# Start agent + Codex sidecar locally (recommended)
start-codex: build-ui
	./scripts/start-codex-local.sh

# Run UI dev server (hot reload) - run agent separately
dev-ui:
	cd ui && npm run dev

# Run tests
test:
	cd agent && pytest

# Verify setup (agent must be running)
verify:
	./scripts/verify.sh
