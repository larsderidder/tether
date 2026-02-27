.PHONY: start start-codex install install-codex build-sidecars verify test

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

# Build TypeScript sidecars into bundled JS for the Python package
build-sidecars:
	./scripts/build-sidecars.sh

# Start agent natively (Claude auto-detect works out of the box)
start: build-sidecars
	cd agent && python -m tether.main

# Start agent + Codex sidecar locally (recommended)
start-codex:
	./scripts/start-codex-local.sh

# Run tests
test:
	cd agent && pytest

# Verify setup (agent must be running)
verify:
	./scripts/verify.sh
