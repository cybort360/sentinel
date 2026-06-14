# SENTINEL Makefile — see CLAUDE.md §8 for the "clean build" contract.
# Fresh clone -> `make setup && make build-sandbox && make demo` must produce
# the full demo trace with no manual steps beyond `.env`.

# uv installs to ~/.local/bin; make sure it's reachable inside recipes.
export PATH := $(HOME)/.local/bin:$(PATH)

.PHONY: setup run run-demo run-web dev bench check check-integration build-sandbox demo web docker-build docker-up deploy help

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup: ## Create venv, install deps (installs uv + checks for Foundry)
	@command -v uv >/dev/null 2>&1 || { \
		echo "[setup] installing uv..."; \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
	}
	uv sync
	@command -v anvil >/dev/null 2>&1 \
		|| echo "[setup] Foundry (anvil) not found — run 'foundryup' before 'make build-sandbox'."

run: setup dev ## Live War Room — real Qwen agents (set QWEN_* in .env; demo fallback if unset)

run-demo: setup demo ## Reproducible demo — deterministic drivers, real sim data, no API keys

run-web: setup web ## Web UI — set up, then serve the War Room at :8088

dev: ## Run the live War Room (real Qwen agents; demo fallback) — auto-sandbox
	@bash sandbox/scripts/with_sandbox.sh uv run python -m demo.run_demo --live

check: ## Lint + format-check + mypy --strict + unit tests (+ coverage gate)
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy
	uv run pytest -m "not integration" \
		--cov=src/sentinel/orchestrator --cov=src/sentinel/agents \
		--cov-report=term-missing --cov-fail-under=80

check-integration: check ## Above + integration tests (requires a running Anvil)
	uv run pytest -m integration

build-sandbox: ## Spin up Anvil, deploy demo contracts, seed state
	@command -v anvil >/dev/null 2>&1 \
		|| { echo "[build-sandbox] Foundry (anvil) not found — run 'foundryup'."; exit 1; }
	bash sandbox/scripts/build_sandbox.sh

demo: ## Run the end-to-end demo trace (auto-starts/stops the sandbox)
	@bash sandbox/scripts/with_sandbox.sh uv run python -m demo.run_demo

web: ## Serve the War Room web UI at :8088 (auto-starts/stops the sandbox)
	@bash sandbox/scripts/with_sandbox.sh uv run python -m demo.web_demo

bench: ## Run the efficiency benchmark (Society vs single-agent Baseline) — auto-sandbox
	@bash sandbox/scripts/with_sandbox.sh uv run python -m demo.benchmark

docker-build: ## Build the docker-compose stack image
	docker compose build

docker-up: ## Run the full stack locally (anvil + 3 MCP servers + orchestrator)
	docker compose up --build

deploy: ## Build + run the compose stack (local, or remote ECS if ECS_HOST set)
	bash deploy/deploy.sh
