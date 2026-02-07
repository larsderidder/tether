#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f "$ROOT/.env" ]]; then
  echo "Missing .env. Create one from .env.example:"
  echo "  cp .env.example .env"
  exit 1
fi

# Export vars from .env for both agent + sidecar.
set -a
source "$ROOT/.env"
set +a

if [[ ! -d "$ROOT/codex-sdk-sidecar/node_modules" ]] || [[ ! -d "$ROOT/codex-src/sdk/typescript/node_modules" ]]; then
  echo "Missing sidecar dependencies. Run:"
  echo "  make install-codex"
  exit 1
fi

SIDECAR_HOST="${TETHER_CODEX_SIDECAR_HOST:-127.0.0.1}"
SIDECAR_PORT="${TETHER_CODEX_SIDECAR_PORT:-8788}"

if ! command -v curl >/dev/null 2>&1; then
  echo "Missing curl. Install it, or replace the health-check in scripts/start-codex-local.sh."
  exit 1
fi

cleanup() {
  if [[ -n "${SIDECAR_PID:-}" ]]; then
    kill "$SIDECAR_PID" >/dev/null 2>&1 || true
    wait "$SIDECAR_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

echo "Starting codex-sdk-sidecar on http://${SIDECAR_HOST}:${SIDECAR_PORT} ..."
(cd "$ROOT/codex-sdk-sidecar" && DOTENV_CONFIG_PATH="$ROOT/.env" npm run start) &
SIDECAR_PID="$!"

echo "Waiting for sidecar health..."
for _ in $(seq 1 100); do
  if curl -fsS "http://${SIDECAR_HOST}:${SIDECAR_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

echo "Starting agent (adapter=codex_sdk_sidecar) ..."
cd "$ROOT/agent"
TETHER_AGENT_ADAPTER=codex_sdk_sidecar python -m tether.main
