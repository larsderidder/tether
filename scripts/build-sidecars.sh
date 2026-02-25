#!/usr/bin/env bash
# Build TypeScript sidecars into single-file bundles for inclusion in the
# Python wheel.  Output goes to agent/tether/sidecars/.
#
# Requirements: Node.js + npm (esbuild is resolved via npx).
#
# The Codex sidecar depends on a local copy of the Codex SDK (codex-src/)
# which is gitignored.  It is built when available and skipped otherwise.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/agent/tether/sidecars"

mkdir -p "$OUT"

BANNER="import { createRequire } from 'module'; const require = createRequire(import.meta.url);"

# Codex sidecar (optional — requires local codex-src checkout)
if [ -d "$ROOT/codex-src/sdk/typescript/src" ]; then
  echo "Building codex-sdk-sidecar bundle..."
  npx --yes esbuild "$ROOT/codex-sdk-sidecar/src/index.ts" \
    --bundle --platform=node --format=esm --target=node18 \
    --banner:js="$BANNER" \
    --outfile="$OUT/codex-sidecar.mjs"
else
  echo "Skipping codex-sdk-sidecar (codex-src/ not present)"
fi

# OpenCode sidecar (always built)
echo "Building opencode-sdk-sidecar bundle..."
npx --yes esbuild "$ROOT/opencode-sdk-sidecar/src/index.ts" \
  --bundle --platform=node --format=esm --target=node18 \
  --banner:js="$BANNER" \
  --outfile="$OUT/opencode-sidecar.mjs"

echo "Sidecar bundles written to $OUT"
ls -lh "$OUT"/*.mjs 2>/dev/null || true
