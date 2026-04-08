#!/usr/bin/env bash
# Deploy local dev wheels to a remote Tether server over SSH,
# and reinstall locally via pipx so the local tether CLI stays in sync.
#
# Usage:
#   scripts/deploy-dev.sh [host]
#
# host defaults to hetzner-01.
#
# What it does:
#   1. Builds wheels for tether-ai and agent-tether from source
#   2. Installs both locally via pipx
#   3. Copies wheels to the remote host
#   4. Installs tether-ai on the remote via pipx (force-reinstall)
#   5. Installs agent-tether into the remote pipx venv (always, since pipx
#      --force wipes the venv and reinstalls from PyPI)
#   6. Restarts the tether systemd service
#   7. Confirms a clean start

set -euo pipefail

REMOTE=${1:-hetzner-01}
TETHER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_TETHER_DIR="/home/lars/xithing/agent-tether"
BUILD_DIR="/tmp/tether-dev-wheels"

# Colours
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}==>${NC} $*"; }
warn()  { echo -e "${YELLOW}  >${NC} $*"; }
die()   { echo -e "${RED}error:${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Build wheels
# ---------------------------------------------------------------------------

info "Building wheels..."

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

info "  tether-ai (${TETHER_DIR}/agent)"
(cd "$TETHER_DIR/agent" && python -m build --wheel --outdir "$BUILD_DIR" -q) \
    || die "tether-ai build failed"

if [[ -d "$AGENT_TETHER_DIR" ]]; then
    info "  agent-tether (${AGENT_TETHER_DIR})"
    (cd "$AGENT_TETHER_DIR" && python -m build --wheel --outdir "$BUILD_DIR" -q) \
        || die "agent-tether build failed"
else
    warn "agent-tether not found at $AGENT_TETHER_DIR, skipping"
fi

TETHER_WHEEL=$(ls "$BUILD_DIR"/tether_ai-*.whl 2>/dev/null | head -1)
AGENT_TETHER_WHEEL=$(ls "$BUILD_DIR"/agent_tether-*.whl 2>/dev/null | head -1)

[[ -n "$TETHER_WHEEL" ]] || die "tether-ai wheel not found after build"

info "Built:"
for whl in "$BUILD_DIR"/*.whl; do
    echo "    $(basename "$whl")"
done

# ---------------------------------------------------------------------------
# 2. Install locally via pipx
# ---------------------------------------------------------------------------

info "Installing locally via pipx..."
# Preserve the active context file across reinstalls — pipx install --force
# wipes the venv but the context file lives in ~/.config/tether/context which
# is outside the venv, so it should survive. However, if pipx somehow resets
# state, we back it up and restore it.
CONTEXT_FILE="$HOME/.config/tether/context"
CONTEXT_BACKUP=""
if [[ -f "$CONTEXT_FILE" ]]; then
    CONTEXT_BACKUP=$(cat "$CONTEXT_FILE")
fi

pipx install --force "$TETHER_WHEEL" 2>&1 \
    | grep -v "^WARNING" \
    | grep -E "(installed|upgraded|error|done|already)" || true

# Restore context file if it was wiped.
if [[ -n "$CONTEXT_BACKUP" && ! -f "$CONTEXT_FILE" ]]; then
    mkdir -p "$(dirname "$CONTEXT_FILE")"
    echo "$CONTEXT_BACKUP" > "$CONTEXT_FILE"
    warn "Restored active context '${CONTEXT_BACKUP}' (was wiped by pipx reinstall)"
fi

if [[ -n "$AGENT_TETHER_WHEEL" ]]; then
    LOCAL_PIPX_PYTHON=$(pipx environment --value PIPX_LOCAL_VENVS)/tether-ai/bin/python
    if [[ -x "$LOCAL_PIPX_PYTHON" ]]; then
        info "  Installing agent-tether into local pipx venv..."
        "$LOCAL_PIPX_PYTHON" -m pip install --force-reinstall --quiet "$AGENT_TETHER_WHEEL" 2>/dev/null \
            || warn "agent-tether local install failed (non-fatal)"
    else
        warn "Local pipx venv python not found at $LOCAL_PIPX_PYTHON"
    fi
fi

# ---------------------------------------------------------------------------
# 3. Copy wheels to remote
# ---------------------------------------------------------------------------

info "Copying wheels to ${REMOTE}:/tmp/..."
scp -q "$BUILD_DIR"/*.whl "${REMOTE}:/tmp/"

# ---------------------------------------------------------------------------
# 4. Install tether-ai on remote via pipx
# ---------------------------------------------------------------------------

TETHER_WHEEL_REMOTE="/tmp/$(basename "$TETHER_WHEEL")"

info "Installing tether-ai on ${REMOTE}..."
# Use pip directly into the existing pipx venv — much faster than pipx install
# --force, which re-resolves and re-downloads all dependencies from PyPI.
# Falls back to pipx install if the venv doesn't exist yet.
ssh "$REMOTE" "
    PIPX_PYTHON=''
    for candidate in \
        /root/.local/share/pipx/venvs/tether-ai/bin/python \
        \$HOME/.local/share/pipx/venvs/tether-ai/bin/python; do
        if [[ -x \"\$candidate\" ]]; then PIPX_PYTHON=\"\$candidate\"; break; fi
    done

    if [[ -n \"\$PIPX_PYTHON\" ]]; then
        \$PIPX_PYTHON -m pip install --force-reinstall --quiet '$TETHER_WHEEL_REMOTE'
        echo 'tether-ai updated in existing pipx venv'
    else
        echo 'No existing pipx venv found, running full pipx install...'
        pipx install '$TETHER_WHEEL_REMOTE'
    fi
"

# ---------------------------------------------------------------------------
# 5. Install agent-tether into remote pipx venv
#    Always required: pipx --force recreates the venv from PyPI, wiping any
#    previously injected wheels.
# ---------------------------------------------------------------------------

if [[ -n "$AGENT_TETHER_WHEEL" ]]; then
    AGENT_TETHER_WHEEL_REMOTE="/tmp/$(basename "$AGENT_TETHER_WHEEL")"
    info "Installing agent-tether into remote pipx venv..."
    # Find the python binary inside the pipx venv; location varies by distro.
    REMOTE_PIPX_PYTHON=$(ssh "$REMOTE" "
        for candidate in \
            /root/.local/share/pipx/venvs/tether-ai/bin/python \
            \$HOME/.local/share/pipx/venvs/tether-ai/bin/python; do
            if [[ -x \"\$candidate\" ]]; then echo \"\$candidate\"; break; fi
        done
    ")
    if [[ -z "$REMOTE_PIPX_PYTHON" ]]; then
        die "Could not find python in remote pipx venv"
    fi
    ssh "$REMOTE" "$REMOTE_PIPX_PYTHON -m pip install --force-reinstall --quiet '$AGENT_TETHER_WHEEL_REMOTE'"
fi

# ---------------------------------------------------------------------------
# 6. Clear bytecode cache and restart service
# ---------------------------------------------------------------------------

info "Restarting tether service..."
ssh "$REMOTE" "
    find /root/.local/share/pipx/venvs/tether-ai -name '*.pyc' -delete 2>/dev/null || true
    find /root/.local/share/pipx/venvs/tether-ai -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    systemctl restart tether
"

# ---------------------------------------------------------------------------
# 7. Confirm clean start
# ---------------------------------------------------------------------------

info "Waiting for startup..."
sleep 4

STARTUP_LOGS=$(ssh "$REMOTE" "journalctl -u tether -n 30 --no-pager 2>/dev/null")

if echo "$STARTUP_LOGS" | grep -q "Application startup failed\|TypeError\|ImportError\|ModuleNotFound"; then
    echo ""
    echo -e "${RED}Service started with errors:${NC}"
    echo "$STARTUP_LOGS" | grep -E "error|Error|TypeError|ImportError|failed|Failed" | head -10
    echo ""
    echo "Full logs: ssh ${REMOTE} journalctl -u tether -n 50"
    exit 1
fi

if echo "$STARTUP_LOGS" | grep -q "UI available"; then
    echo ""
    echo -e "${GREEN}Deployed successfully.${NC} Tether is running on ${REMOTE}."
else
    warn "Service started but 'UI available' not seen yet. Check logs:"
    warn "  ssh ${REMOTE} journalctl -u tether -f"
fi
