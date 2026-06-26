from __future__ import annotations

import subprocess
from pathlib import Path

_DEMO_CONTRACTS = [
    "contracts/SubscriptionBilling.sol",
    "contracts/SubscriptionBillingGuarded.sol",
    "contracts/ReentrancyAttacker.sol",
    "contracts/YieldVault.sol",
]


def build_demo_contracts(sandbox: Path) -> None:
    subprocess.run(["forge", "build", *_DEMO_CONTRACTS], cwd=sandbox, check=True)
