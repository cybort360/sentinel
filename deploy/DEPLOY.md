# SENTINEL — Alibaba Cloud Deployment & Verification

This is the operational companion to `architecture.md` §15. It covers (1) running
the whole stack locally, (2) standing it up on an Alibaba Cloud ECS instance, and
(3) capturing the **separate verification recording** the hackathon requires to
prove the backend runs on Alibaba Cloud.

The deployment artifact is one `docker-compose.yml` (repo root). Local dev *is*
the deployment — the same stack runs in both places.

---

## What the stack contains

| Service | Image command | Role |
|---|---|---|
| `anvil` | `anvil --host 0.0.0.0 --port 8545` | Local EVM sandbox — the only transaction broadcast target (Rule 3). Publishes `127.0.0.1:8545` for operator inspection. |
| `simulation-mcp` | `python -m sentinel.mcp_servers.simulation_mcp.server` | SimulationMCP as a FastMCP HTTP endpoint. Joins anvil's netns so it reaches it at `127.0.0.1` (Rule 3 — see below). |
| `codebase-mcp` | `python -m sentinel.mcp_servers.codebase_mcp.server` | CodebaseMCP as a FastMCP HTTP endpoint. |
| `memory-mcp` | `python -m sentinel.mcp_servers.memory_mcp.server` | MemoryMCP as a FastMCP HTTP endpoint, backed by the `memory-data` volume. |
| `orchestrator` | `python -m demo.run_demo` | Runs the end-to-end §12 demo against the live Anvil, then exits 0. |

**Honest note on the MCP services.** The reproducible `make demo` trace runs the
engines *in-process* for determinism (there are no Qwen credentials in this
environment — see `demo/run_demo.py`). The three MCP services run the **same
engines** wrapped as real networked FastMCP endpoints. The stack stands them up
to prove the custom MCP servers (the Track 3 "custom MCP integrations" lever)
deploy and run on the Alibaba Cloud host — they are not on the demo's critical
path, which is why the orchestrator depends only on `anvil`.

**Rule 3 and Docker networking.** `simulation_mcp.config.assert_local_rpc` only
permits loopback hosts, so a service-DNS RPC like `http://anvil:8545` is rejected
*by design*. `simulation-mcp` and `orchestrator` therefore use
`network_mode: "service:anvil"` — they share Anvil's network namespace and reach
it at `127.0.0.1:8545`. The safety boundary stays genuine; no allowlist is
widened to make Docker work.

---

## 1. Run it locally

```bash
cp .env.example .env        # optional: set ANVIL_FORK_URL for realistic fork state
make docker-up              # docker compose up --build (foreground)
# or detached:
make deploy                 # no ECS_HOST -> builds + runs locally, prints `ps`
docker compose logs -f orchestrator
```

Expected: `anvil`, `simulation-mcp`, `codebase-mcp`, `memory-mcp` reach
`healthy`; `orchestrator` prints the two-session War Room trace, the §11
efficiency table, the cross-session memory line, then exits `0`.

Tear down: `docker compose down -v` (the `-v` also drops the memory volume).

---

## 2. Provision the Alibaba Cloud ECS instance (one-time)

1. **Create an ECS instance.** Ubuntu 22.04 LTS, `ecs.g7.large` (2 vCPU / 8 GiB)
   is comfortable; `t6` burstable works for a skeleton. Assign a public IP.
2. **Security group.** Allow inbound SSH (22) **from your operator IP only**.
   Do **not** open 8545 or the MCP ports to the internet — they are published to
   host loopback / kept internal to the stack (§15: "exposed only to the demo
   operator"). The verification clip is recorded over SSH, so no extra ports are
   needed.
3. **Install Docker Engine + compose plugin** on the host:
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker "$USER"   # re-login so `docker` works without sudo
   docker compose version            # confirm the compose plugin is present
   ```
4. **(Optional) create `.env` on the host** with `ANVIL_FORK_URL` and, later, the
   real `QWEN_*` credentials. Secrets live only on the host — `deploy.sh` never
   ships your local `.env`.

## 3. Deploy from your laptop

```bash
# point at the host (or put ECS_HOST / ECS_REMOTE_DIR in .env)
ECS_HOST=ubuntu@<ecs-public-ip> make deploy
```

`deploy/deploy.sh` rsyncs the repo (excluding `.env`, `.git`, caches, and
runtime state), then runs `docker compose up -d --build` on the host and prints
the service health table. First build pulls Foundry + deps and runs
`forge build`, so expect a few minutes; subsequent deploys are incremental.

Keep the stack running throughout development (architecture.md §15) and redeploy
with the same command whenever the stack changes.

## 4. Record the verification clip

The hackathon wants proof the backend runs on Alibaba Cloud. Record a short
screen capture of an SSH session to the ECS instance showing:

```bash
ssh ubuntu@<ecs-public-ip>
hostname && curl -s http://100.100.100.200/latest/meta-data/region-id   # ECS metadata: proves it's an Alibaba Cloud host
cd ~/sentinel
docker compose ps                              # all services Up/healthy
docker compose logs --tail=80 orchestrator     # the live §12 demo trace
cast block-number --rpc-url http://127.0.0.1:8545   # the sandbox is live on the host
```

`100.100.100.200` is the Alibaba Cloud ECS instance metadata endpoint;
`region-id` returning an Alibaba region (e.g. `ap-southeast-1`) on camera is the
cleanest proof of the host. Re-record once at skeleton stage and again at final
submission if the stack changed meaningfully.

---

## Troubleshooting

- **`orchestrator` exits non-zero / can't reach Anvil.** Confirm `anvil` is
  `healthy` first (`docker compose ps`); the orchestrator waits on it via
  `depends_on: service_healthy`. Check `docker compose logs anvil`.
- **`forge build` fails during image build.** Usually a transient solc download;
  re-run `make docker-build`. The build needs outbound network for Foundry/solc.
- **MemoryMCP data reset.** The store persists in the `memory-data` volume;
  `docker compose down -v` wipes it. Use plain `down` to keep it.
- **`NonLocalRPCError` on `simulation-mcp` start.** It must share anvil's netns
  (`network_mode: service:anvil`) with `ANVIL_HOST=127.0.0.1`; don't change those
  to a service name — that's the Rule 3 boundary doing its job.
