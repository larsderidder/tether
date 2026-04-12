.PHONY: start start-codex start-opencode install install-codex install-opencode build-ui verify dev-ui test

# =============================================================================
# Native mode (recommended)
# =============================================================================

# Install dependencies (run once)
install:
	cd agent && pip install -e ".[dev]"
	cd ui && npm ci

install-sidecars:
	cd codex-src/sdk/typescript && npm install --ignore-scripts
	npm install --workspaces

install-codex: install-sidecars
install-opencode: install-sidecars

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

# Start agent with the managed OpenCode sidecar
start-opencode: build-ui build-sidecars
	./scripts/start-opencode-local.sh

# Run UI dev server (hot reload) - run agent separately
dev-ui:
	cd ui && npm run dev

# Run tests
test:
	cd agent && pytest

# Verify setup (agent must be running)
verify:
	./scripts/verify.sh
