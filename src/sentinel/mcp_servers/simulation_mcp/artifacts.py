"""Load Foundry-compiled contract artifacts (ABI + bytecode) for deployment.

Foundry writes artifacts to ``<out>/<File>.sol/<Name>.json``. This module
resolves a contract identifier — ``"Name"`` or ``"File.sol:Name"`` — to that
artifact and extracts the ABI and creation bytecode for web3.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ContractArtifact:
    """A compiled contract's ABI and creation bytecode."""

    name: str
    abi: list[Any]
    bytecode: str


def _split_identifier(contract: str) -> tuple[str, str]:
    """Resolve a contract identifier to its (sol filename, contract name).

    Args:
        contract: ``"Name"``, ``"Name.sol"``, or ``"File.sol:Name"``.

    Returns:
        A ``(sol_filename, contract_name)`` pair, e.g. ``("Foo.sol", "Foo")``.
    """
    if ":" in contract:
        file_part, name = contract.split(":", 1)
        sol_file = file_part if file_part.endswith(".sol") else f"{file_part}.sol"
        return sol_file, name
    name = contract[:-4] if contract.endswith(".sol") else contract
    return f"{name}.sol", name


def load_artifact(contract: str, artifacts_dir: Path) -> ContractArtifact:
    """Load a compiled contract artifact from the Foundry output directory.

    Args:
        contract: Contract identifier (see :func:`_split_identifier`).
        artifacts_dir: The Foundry ``out`` directory.

    Returns:
        The parsed :class:`ContractArtifact`.

    Raises:
        FileNotFoundError: If the artifact is missing (run ``forge build``).
    """
    sol_file, name = _split_identifier(contract)
    path = artifacts_dir / sol_file / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"no artifact for {contract!r} at {path} — run `forge build` in "
            f"sandbox/ (or `make build-sandbox`) first"
        )
    data = json.loads(path.read_text())
    bytecode = data["bytecode"]["object"]
    return ContractArtifact(name=name, abi=list(data["abi"]), bytecode=str(bytecode))
