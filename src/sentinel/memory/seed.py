"""Seed MemoryMCP with prior-incident records for the demo (architecture.md §6.4).

These are synthetic-but-simulation-shaped post-mortems from *prior* audit runs on
*different* contracts. They prove cross-session memory generalises: running on
Contract 1 (SubscriptionBilling) surfaces the batching/latency record, while
running on Contract 2 (YieldVault) surfaces the access-control record — neither
is wired to a single demo path. A third (oracle) record acts as a distractor so
retrieval ranking has to discriminate.

Run as a script to populate the configured DB::

    uv run python -m sentinel.memory.seed
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sentinel.mcp_servers.memory_mcp.config import MemoryConfig
from sentinel.memory.schema import MemoryRecord
from sentinel.memory.store import MemoryStore
from sentinel.observability.trace_logger import get_logger
from sentinel.orchestrator.schema import Severity

_log = get_logger("memory_mcp.seed")

# Stable ids so re-seeding upserts rather than duplicating.
_BATCHING_ID = "00000000-0000-0000-0000-0000000000a1"
_ACCESS_CONTROL_ID = "00000000-0000-0000-0000-0000000000a2"
_ORACLE_ID = "00000000-0000-0000-0000-0000000000a3"

_NOW = datetime.now(tz=UTC)


def seed_records() -> list[MemoryRecord]:
    """Return the demo seed records (architecture.md §6.4)."""
    return [
        # Tied to Contract 1 (SubscriptionBilling): the batching/latency tension
        # that the War Room negotiates in §12 round 2.
        MemoryRecord(
            id=_BATCHING_ID,
            created_at=_NOW - timedelta(days=30),
            topic_tags=["batching", "settlement-latency", "congestion", "fee-spike"],
            severity=Severity.HIGH,
            description=(
                "A prior settlement protocol introduced transaction batching to "
                "absorb the fee-spike congestion that was causing settlement "
                "reverts under load."
            ),
            lesson_text=(
                "Batching to defeat fee-spike congestion introduces a silent "
                "finality/settlement delay: congestion risk and latency risk "
                "trade off against each other and cannot be jointly minimised. "
                "Any batching fix must disclose the added settlement delay."
            ),
            source_run_id="seed-run-batching-0001",
        ),
        # Tied to Contract 2 (YieldVault): the access-control vulnerability class.
        MemoryRecord(
            id=_ACCESS_CONTROL_ID,
            created_at=_NOW - timedelta(days=45),
            topic_tags=["access-control", "privileged-functions", "fund-custody"],
            severity=Severity.HIGH,
            description=(
                "A vault's emergency-withdraw and operator-assignment functions "
                "lacked owner authorization, letting any caller reassign the "
                "operator role and then sweep custody of all deposits."
            ),
            lesson_text=(
                "Guarding only the withdraw path is insufficient: if the "
                "role-assignment path (setOperator) is left ungated, an attacker "
                "grants themselves the role and drains anyway. Every privileged "
                "fund-moving path AND the role-assignment path must be gated."
            ),
            source_run_id="seed-run-access-0001",
        ),
        # Distractor: a different vulnerability class (oracle manipulation).
        MemoryRecord(
            id=_ORACLE_ID,
            created_at=_NOW - timedelta(days=60),
            topic_tags=["oracle", "price-manipulation"],
            severity=Severity.MEDIUM,
            description=(
                "An AMM spot-price oracle was manipulable within a single block "
                "using flash liquidity to move the reference price."
            ),
            lesson_text=(
                "Spot prices read from a single AMM pool can be moved within one "
                "transaction; prefer time-weighted averages or multiple "
                "independent sources."
            ),
            source_run_id="seed-run-oracle-0001",
        ),
    ]


def seed_store(store: MemoryStore) -> list[MemoryRecord]:
    """Write the demo seed records into ``store`` (idempotent)."""
    records = seed_records()
    for record in records:
        store.write_memory(record)
    return records


def main() -> None:
    """Seed the configured MemoryMCP database."""
    config = MemoryConfig.from_env()
    store = MemoryStore(config.db_path)
    try:
        records = seed_store(store)
    finally:
        store.close()
    _log.info("seed_complete", db_path=config.db_path, count=len(records))


if __name__ == "__main__":
    main()
