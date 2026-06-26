"""Load Foundry-compiled contract artifacts (ABI + bytecode) for deployment.

Foundry writes artifacts to ``<out>/<File>.sol/<Name>.json``. This module
resolves a contract identifier — ``"Name"``, ``"File.sol:Name"``, or a source
path such as ``"sandbox/contracts/File.sol"`` — to that artifact and extracts
the ABI and creation bytecode for web3.py.
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
        contract: ``"Name"``, ``"Name.sol"``, source path, or
            ``"File.sol:Name"``.

    Returns:
        A ``(sol_filename, contract_name)`` pair, e.g. ``("Foo.sol", "Foo")``.
    """
    if ":" in contract:
        file_part, name = contract.split(":", 1)
        stem = Path(file_part).name
        sol_file = stem if stem.endswith(".sol") else f"{stem}.sol"
        return sol_file, name
    stem = Path(contract).name
    name = stem[:-4] if stem.endswith(".sol") else stem
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
    if not path.is_file() and ":" not in contract:
        path = _artifact_for_source_file(contract, artifacts_dir, sol_file, name)
    if not path.is_file():
        raise FileNotFoundError(
            f"no artifact for {contract!r} at {path} — run `forge build` in "
            f"sandbox/ (or `make build-sandbox`) first"
        )
    data = json.loads(path.read_text())
    bytecode = data["bytecode"]["object"]
    return ContractArtifact(
        name=path.stem, abi=list(data["abi"]), bytecode=str(bytecode)
    )


def _artifact_for_source_file(
    contract: str, artifacts_dir: Path, sol_file: str, requested_name: str
) -> Path:
    """Find the artifact compiled from a source file when names differ."""
    artifact_dir = artifacts_dir / sol_file
    fallback = artifact_dir / f"{requested_name}.json"
    if not artifact_dir.is_dir():
        return fallback
    candidates: list[Path] = []
    for candidate in artifact_dir.glob("*.json"):
        try:
            data = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        bytecode = data.get("bytecode", {})
        if isinstance(bytecode, dict) and str(bytecode.get("object", "")):
            candidates.append(candidate)
    if len(candidates) == 1:
        return candidates[0]
    return fallback
