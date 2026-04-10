#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="${HOME}/.virtualenvs/tether/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Missing Python interpreter at $VENV_PYTHON"
  exit 1
fi

cd "$ROOT"

export PATH="${HOME}/.virtualenvs/tether/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
export PYTHONUNBUFFERED=1

if [[ ! -f "$ROOT/.env" ]]; then
  echo "Missing $ROOT/.env"
  exit 1
fi

make build-ui build-sidecars

cd "$ROOT/agent"
exec "$VENV_PYTHON" -m tether.main
