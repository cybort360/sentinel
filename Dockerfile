# SENTINEL runtime image (architecture.md §15).
#
# One image serves every Python service in the compose stack (orchestrator +
# the three MCP servers) and also provides the Foundry toolchain (anvil/cast/
# forge) that the `anvil` service runs. Building the sandbox artifacts at image
# build time means the SimulationEngine can deploy contract bytecode at runtime
# without a live `forge build`.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # venv (created by `uv sync`) + Foundry binaries on PATH for all services.
    PATH="/app/.venv/bin:/root/.foundry/bin:${PATH}"

# System deps:
#   git  — CodebaseMCP stages patches as git branches (architecture.md §5.1)
#   curl — Foundry installer
#   ca-certificates — TLS for the installer + any read-only fork RPC
# build-essential is intentionally omitted: every Python dependency ships wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv: pinned binary from the official image (no pip bootstrap needed).
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

# Foundry (anvil, cast, forge) — the local EVM sandbox toolchain (Rule 3 target).
RUN curl -L https://foundry.paradigm.xyz | bash && foundryup

WORKDIR /app

# --- Dependency layer (cached unless the lockfile/manifest changes) -----------
# Runtime services need only the main dependency group (no ruff/mypy/pytest).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# --- Project + sandbox --------------------------------------------------------
COPY . .
# Install the sentinel package, then compile the demo contracts so the
# SimulationEngine finds bytecode in sandbox/out at runtime.
RUN uv sync --frozen --no-dev \
    && forge build --root sandbox \
        contracts/SubscriptionBilling.sol \
        contracts/SubscriptionBillingGuarded.sol \
        contracts/ReentrancyAttacker.sol \
        contracts/YieldVault.sol

# Default command runs the end-to-end demo (architecture.md §12); each compose
# service overrides this with its own entrypoint.
CMD ["python", "-m", "demo.run_demo"]
