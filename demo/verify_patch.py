"""Patch-verification demo: *prove* a proposed fix closes the hole (§5.2).

The autopilot credibility moment — instead of only *suggesting* a fix, SENTINEL
compiles the patched contract, deploys it, and re-runs the exact exploit that
worked on the vulnerable one. Here: a reentrancy drain lands on
``SubscriptionBilling`` and is *blocked* on the reentrancy-guarded patch, with
the before/after read straight off a live Anvil run (Rule 1).

Run with ``make verify`` (auto-manages the sandbox).
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from sentinel.mcp_servers.simulation_mcp.config import SimulationConfig
from sentinel.mcp_servers.simulation_mcp.engine import SimulationEngine
from sentinel.mcp_servers.simulation_mcp.results import PatchVerification
from sentinel.observability.trace_logger import DemoTag, emit, get_logger

_log = get_logger("verify")
_REPO_ROOT = Path(__file__).resolve().parents[1]

_VULNERABLE = "SubscriptionBilling"
_FIXED = "SubscriptionBillingGuarded"
_EXPLOIT = "reentrancy_drain"


def _eth(wei: int) -> str:
    """Format wei as a short ETH string."""
    return f"{wei / 10**18:.3f} ETH"


def _render(console: Console, v: PatchVerification) -> None:
    """Render the before/after exploit comparison + the verdict."""
    table = Table(
        title=f"Patch verification — exploit: {v.exploit}", show_lines=True
    )
    table.add_column("Contract")
    table.add_column("Exploit lands?")
    table.add_column("Funds drained")
    table.add_column("Attack tx")
    for label, r in (("vulnerable", v.before), ("patched", v.after)):
        table.add_row(
            f"{r.contract}\n[dim]({label})[/]",
            "[red]YES — exploited[/]" if r.exploited else "[green]NO — blocked[/]",
            _eth(r.drained_wei),
            "reverted" if r.reverted else "succeeded",
        )
    console.print(table)
    if v.fix_verified:
        console.print(
            f"[bold green]✓ FIX VERIFIED[/] — the reentrancy guard blocks the drain "
            f"that stole {_eth(v.before.drained_wei)} from the vulnerable contract."
        )
    else:
        console.print("[bold red]✗ NOT VERIFIED[/] — the patch did not close the hole.")


def _emit_summary(v: PatchVerification) -> None:
    """Emit a structured §10 summary of the verification."""
    emit(
        _log,
        DemoTag.SYSTEM_DECISION,
        (
            f"[PATCH VERIFIED] {v.exploit}: {v.vulnerable_contract} exploited "
            f"(drained {_eth(v.before.drained_wei)}) -> {v.fixed_contract} blocked"
            if v.fix_verified
            else f"[PATCH NOT VERIFIED] {v.exploit}: fix did not close the hole"
        ),
        agent="orchestrator",
        action="verify_patch",
        trace_id=v.trace_id,
        fix_verified=v.fix_verified,
    )


def _write_artifact(v: PatchVerification) -> Path:
    """Persist the verification under demo/scenarios/ for the submission."""
    path = _REPO_ROOT / "demo" / "scenarios" / "patch_verification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(v.model_dump_json(indent=2))
    return path


def main() -> None:
    """CLI entrypoint for ``make verify``."""
    console = Console()
    engine = SimulationEngine(SimulationConfig.from_env())
    console.rule("[bold]Patch verification — reentrancy guard")
    verification = engine.verify_patch(_VULNERABLE, _FIXED, _EXPLOIT)
    _render(console, verification)
    _emit_summary(verification)
    path = _write_artifact(verification)
    console.print(f"[green]Verification complete.[/] Report written to {path}")


if __name__ == "__main__":
    main()
