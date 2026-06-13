#!/usr/bin/env bash
#
# build_sandbox.sh — boot a local Anvil instance and deploy the SENTINEL demo
# contracts to it (architecture.md §5.2, §8, §12).
#
# Rule 3 (CLAUDE.md Golden Rule #3): this only ever talks to a LOCAL Anvil node
# on 127.0.0.1. The deployer key is read from Anvil's own startup log — it is a
# throwaway dev key for this local node, never a real funded key. If
# ANVIL_FORK_URL is set it is used READ-ONLY to fork mainnet state; all
# transactions still go to the local node.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SANDBOX="$ROOT/sandbox"

# Load .env (ANVIL_HOST, ANVIL_PORT, ANVIL_FORK_URL) if present.
if [ -f "$ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/.env"
    set +a
fi

ANVIL_HOST="${ANVIL_HOST:-127.0.0.1}"
ANVIL_PORT="${ANVIL_PORT:-8545}"
RPC_URL="http://${ANVIL_HOST}:${ANVIL_PORT}"

PID_FILE="$SANDBOX/.anvil.pid"
LOG_FILE="$SANDBOX/.anvil.log"
DEPLOY_DIR="$SANDBOX/deployments"
DEPLOY_FILE="$DEPLOY_DIR/local.json"

log() { printf '[build-sandbox] %s\n' "$1"; }

# --- 1. Stop any Anvil we previously started (fresh sandbox state each run) ----
if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        log "stopping previous Anvil (pid $OLD_PID)"
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PID_FILE"
fi

# --- 2. Boot Anvil ------------------------------------------------------------
ANVIL_ARGS=(--host "$ANVIL_HOST" --port "$ANVIL_PORT")
if [ -n "${ANVIL_FORK_URL:-}" ]; then
    log "forking from \$ANVIL_FORK_URL (read-only)"
    ANVIL_ARGS+=(--fork-url "$ANVIL_FORK_URL")
fi

log "starting Anvil on $RPC_URL"
anvil "${ANVIL_ARGS[@]}" >"$LOG_FILE" 2>&1 &
ANVIL_PID=$!
echo "$ANVIL_PID" >"$PID_FILE"

# --- 3. Wait for the RPC to come up ------------------------------------------
ready=0
for _ in $(seq 1 60); do
    if ! kill -0 "$ANVIL_PID" 2>/dev/null; then
        log "ERROR: Anvil exited during startup. Last log lines:"
        tail -n 20 "$LOG_FILE" >&2
        exit 1
    fi
    if cast block-number --rpc-url "$RPC_URL" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 0.5
done
if [ "$ready" -ne 1 ]; then
    log "ERROR: Anvil did not become ready in time. Last log lines:"
    tail -n 20 "$LOG_FILE" >&2
    kill "$ANVIL_PID" 2>/dev/null || true
    exit 1
fi
log "Anvil ready (pid $ANVIL_PID)"

# --- 4. Pull a local dev key + a distinct merchant account from the log -------
DEPLOYER_PK="$(grep -oE '0x[a-fA-F0-9]{64}' "$LOG_FILE" | head -n 1)"
MERCHANT="$(grep -oE '0x[a-fA-F0-9]{40}' "$LOG_FILE" | sed -n '2p')"
if [ -z "$DEPLOYER_PK" ] || [ -z "$MERCHANT" ]; then
    log "ERROR: could not read dev key/accounts from Anvil log"
    exit 1
fi
DEPLOYER="$(cast wallet address --private-key "$DEPLOYER_PK")"
log "deployer  = $DEPLOYER"
log "merchant  = $MERCHANT"

# --- 5. Build + deploy both contracts ----------------------------------------
cd "$SANDBOX"
log "compiling contracts"
forge build >/dev/null

deploy() {
    # $1 = "path:Name", remaining args = constructor args
    local target="$1"
    shift
    local args=(forge create "$target"
        --rpc-url "$RPC_URL"
        --private-key "$DEPLOYER_PK"
        --broadcast --json)
    if [ "$#" -gt 0 ]; then
        args+=(--constructor-args "$@")
    fi
    local out
    out="$("${args[@]}")"
    echo "$out" | grep -oE '"deployedTo": *"0x[0-9a-fA-F]{40}"' | grep -oE '0x[0-9a-fA-F]{40}'
}

log "deploying SubscriptionBilling(merchant=$MERCHANT)"
BILLING_ADDR="$(deploy "contracts/SubscriptionBilling.sol:SubscriptionBilling" "$MERCHANT")"
log "  -> $BILLING_ADDR"

log "deploying YieldVault()"
VAULT_ADDR="$(deploy "contracts/YieldVault.sol:YieldVault")"
log "  -> $VAULT_ADDR"

if [ -z "$BILLING_ADDR" ] || [ -z "$VAULT_ADDR" ]; then
    log "ERROR: a deployment returned no address"
    exit 1
fi

# --- 6. Record deployment artifact for SimulationMCP -------------------------
mkdir -p "$DEPLOY_DIR"
cat >"$DEPLOY_FILE" <<JSON
{
  "rpc_url": "$RPC_URL",
  "chain_id": $(cast chain-id --rpc-url "$RPC_URL"),
  "deployer": "$DEPLOYER",
  "contracts": {
    "SubscriptionBilling": {
      "address": "$BILLING_ADDR",
      "constructor_args": { "merchant": "$MERCHANT" }
    },
    "YieldVault": {
      "address": "$VAULT_ADDR",
      "constructor_args": {}
    }
  }
}
JSON

log "wrote $DEPLOY_FILE"
log "sandbox ready. Anvil pid $ANVIL_PID is still running on $RPC_URL"
log "stop it with: kill \$(cat $PID_FILE)"
