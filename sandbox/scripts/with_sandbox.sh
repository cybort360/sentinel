#!/usr/bin/env bash
#
# with_sandbox.sh — make the demo / web UI "just work" from one command.
#
# Ensures the local Anvil sandbox is up, runs the given command against it, then
# stops the sandbox IFF this script started it (a sandbox you launched yourself
# via `make build-sandbox` is left running). Rule 3 is unchanged — this only
# manages the local node's lifecycle.
#
# Usage: bash sandbox/scripts/with_sandbox.sh <command...>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# Honor ANVIL_HOST/ANVIL_PORT from .env if present (build_sandbox.sh does too).
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi
RPC="http://${ANVIL_HOST:-127.0.0.1}:${ANVIL_PORT:-8545}"

started=0
if cast block-number --rpc-url "$RPC" >/dev/null 2>&1; then
    echo "[sentinel] using the Anvil sandbox already running on $RPC"
else
    echo "[sentinel] starting the Anvil sandbox..."
    make build-sandbox
    started=1
fi

cleanup() {
    if [ "$started" -eq 1 ] && [ -f sandbox/.anvil.pid ]; then
        echo "[sentinel] stopping the sandbox..."
        kill "$(cat sandbox/.anvil.pid)" 2>/dev/null || true
        rm -f sandbox/.anvil.pid
    fi
}
trap cleanup EXIT INT TERM

"$@"
