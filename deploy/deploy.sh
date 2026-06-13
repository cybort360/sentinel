#!/usr/bin/env bash
#
# deploy.sh — bring the SENTINEL compose stack up locally, or on an Alibaba
# Cloud ECS host (architecture.md §15).
#
# Local (default):   make deploy
# Remote ECS:        ECS_HOST=root@1.2.3.4 make deploy
#
# Remote mode rsyncs the repo to the ECS host (respecting .gitignore-style
# excludes) and runs `docker compose up -d --build` there over SSH, then prints
# the service health table — the artifact the hackathon verification clip
# records. No secrets are baked into the image; the host's own .env (if any) is
# left untouched.
#
# Rule 3 (CLAUDE.md): nothing here points SimulationMCP at a non-local RPC. Anvil
# stays inside the stack; only the operator-loopback 8545 port is published.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Load ECS_HOST / ECS_REMOTE_DIR from .env if present (see .env.example).
if [ -f "$ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/.env"
    set +a
fi

REMOTE_DIR="${ECS_REMOTE_DIR:-~/sentinel}"

log() { printf '[deploy] %s\n' "$1"; }

# --- Local mode ---------------------------------------------------------------
if [ -z "${ECS_HOST:-}" ]; then
    log "no ECS_HOST set — building and running the stack locally"
    docker compose up -d --build
    log "stack status:"
    docker compose ps
    log "follow the demo trace with: docker compose logs -f orchestrator"
    exit 0
fi

# --- Remote mode (Alibaba Cloud ECS) -----------------------------------------
log "deploying to ECS host: $ECS_HOST (dir: $REMOTE_DIR)"

# 1. Sync the repo to the host (exclude host-only state; .env is NOT shipped —
#    fill it in on the host so secrets never leave it).
rsync -az --delete \
    --exclude '.git' \
    --exclude '.env' \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '.mypy_cache' \
    --exclude '.ruff_cache' \
    --exclude '.pytest_cache' \
    --exclude 'data' \
    --exclude 'sandbox/out' \
    --exclude 'sandbox/cache' \
    --exclude 'sandbox/.anvil.log' \
    --exclude 'sandbox/.anvil.pid' \
    "$ROOT"/ "$ECS_HOST:$REMOTE_DIR"/

# 2. Build + (re)start the stack on the host.
# shellcheck disable=SC2029
ssh "$ECS_HOST" "cd $REMOTE_DIR && docker compose up -d --build && docker compose ps"

log "done. Record the verification clip with:"
log "  ssh $ECS_HOST 'cd $REMOTE_DIR && docker compose ps && docker compose logs --tail=80 orchestrator'"
