#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f "$ROOT/.env" ]]; then
  echo "Missing .env. Create one from .env.example:"
  echo "  cp .env.example .env"
  exit 1
fi

set -a
source "$ROOT/.env"
set +a

if [[ ! -d "$ROOT/node_modules" ]]; then
  echo "Missing workspace dependencies. Run:"
  echo "  make install-opencode"
  exit 1
fi

OPENCODE_BIN_CANDIDATE="${OPENCODE_BIN:-}"
if [[ -z "$OPENCODE_BIN_CANDIDATE" && -x "$HOME/.opencode/bin/opencode" ]]; then
  OPENCODE_BIN_CANDIDATE="$HOME/.opencode/bin/opencode"
fi
if [[ -n "$OPENCODE_BIN_CANDIDATE" ]]; then
  export OPENCODE_BIN="$OPENCODE_BIN_CANDIDATE"
fi

if ! command -v opencode >/dev/null 2>&1 && [[ -z "${OPENCODE_BIN:-}" ]]; then
  echo "Missing opencode CLI. Install it or set OPENCODE_BIN before running start-opencode."
  exit 1
fi

echo "Starting agent (adapter=opencode, managed sidecar) ..."
cd "$ROOT/agent"
TETHER_AGENT_ADAPTER=opencode python -m tether.main
