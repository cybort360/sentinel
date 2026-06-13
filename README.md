# SENTINEL

A multi-agent "war room" that argues about whether your protocol is safe to
ship — using real simulation data, real institutional memory, and a real human
in the loop for the decisions that matter.

Submitted to the Qwen Cloud Global AI Hackathon, Track 3 (Agent Society)
primary, with Track 1 (Memory) and Track 4 (Autopilot) as integrated
sub-tracks.

- **What it is / how it works:** see [`architecture.md`](architecture.md).
- **How we build it (rules, tooling, DoD):** see [`CLAUDE.md`](CLAUDE.md).
- **How to deploy + record the Alibaba Cloud verification:** see
  [`deploy/DEPLOY.md`](deploy/DEPLOY.md).

## Quick start

```bash
make setup          # create venv via uv, install deps, check for Foundry
make check          # lint + format-check + mypy --strict + unit tests
make build-sandbox  # spin up Anvil and deploy the demo contracts
make demo           # run the end-to-end §12 demo trace against the real sandbox
```

`make demo` runs the two-session War Room end to end against a live Anvil sandbox
(real simulation data, real institutional memory, a real blocking human
checkpoint), then prints the [`architecture.md`](architecture.md) §11 efficiency
table. The checkpoint auto-approves with a clearly-labelled demo decision; pass
`--interactive` (`uv run python -m demo.run_demo --interactive`) for the real
`rich` prompt.

Requires Python 3.11+ (provisioned by `uv`) and Foundry (`foundryup`) for the
sandbox. Copy `.env.example` to `.env` to set `ANVIL_FORK_URL` (read-only fork
state) and, for the live LLM agents, the `QWEN_*` credentials.

## Web UI (browser War Room)

```bash
make build-sandbox  # Anvil + demo contracts
make web            # serve the War Room at http://localhost:8088
```

A single-page view of the same audit: the War Room timeline streams live (SSE),
every cited number is one click from the real `SimulationMCP` result behind it
(Golden Rule #1, made visible), and the Human Checkpoint is driven by a button
that genuinely blocks the run (Golden Rule #2). It's a *read-only renderer* of
the §10 trace stream — it never originates an agent claim. Design and internals:
[`architecture.md`](architecture.md) §18.

## Run the full stack in Docker

```bash
make docker-up      # anvil + 3 MCP servers + orchestrator (architecture.md §15)
```

The same `docker-compose.yml` is the Alibaba Cloud deployment artifact —
`ECS_HOST=user@ip make deploy` ships it to an ECS instance. See
[`deploy/DEPLOY.md`](deploy/DEPLOY.md) for provisioning and the verification clip.

## Status

The War Room (Yield / Adversary / Arbitrator / Lessons / Baseline agents), all
three custom MCP servers, the decaying memory store, the blocking human
checkpoint, the orchestration graph, and the end-to-end demo are implemented and
covered by unit + integration (real-Anvil) + golden-trace tests. Note: in this
build the demo agents are deterministic drivers making **real** `SimulationMCP`
calls (Golden Rule 1 preserved) — wiring the live Qwen models behind the same
agent interfaces is the remaining step before final submission.

## License

[MIT](LICENSE). `CodebaseMCP` and `SimulationMCP` are intended to be useful
standalone to any team auditing Solidity/EVM code (architecture.md §16).
