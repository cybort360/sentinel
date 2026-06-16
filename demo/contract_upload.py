"""Compile-on-demand for browser-uploaded contracts (architecture.md §18).

This is the Phase-2 plumbing behind the War Room's "Upload .sol" picker: it takes
a contract a user opened from their own machine, drops it into the Foundry
sandbox, compiles it with ``forge build``, and resolves the compiled artifact so
:class:`SimulationEngine` can deploy it to the local Anvil fork. It is the
SENTINEL-specific wiring injected into the generic web app, so the dependency
only ever points demo -> src.

Golden Rules: the contract is written under the sandbox sources and compiled +
deployed to the **local fork only** (Rule 3 — nothing here touches a real
network). A full adversarial audit of arbitrary code still needs the live Qwen
agents; this module only proves the read -> compile -> deploy -> gas pipeline.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sentinel.web import CompileError, UploadRejectedError

#: Allowed contract filename stem: keeps the write inside the sandbox (no path
#: traversal, no shell-meaningful characters) and matches Solidity identifiers.
_SAFE_STEM = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_FORGE_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class UploadedContract:
    """A compiled, deployable upload: its sandbox path and contract name."""

    path: str  #: Repo-relative source path (e.g. ``sandbox/contracts/Foo.sol``).
    contract: str  #: The Solidity contract name (artifact + deploy identifier).
    label: str  #: Display label for the picker.


def safe_stem(filename: str) -> str:
    """Return a validated bare contract name from an uploaded filename.

    Args:
        filename: The browser-supplied filename (e.g. ``MyVault.sol``).

    Returns:
        The validated stem (``MyVault``).

    Raises:
        UploadRejectedError: If the name isn't a ``.sol`` file with a safe stem.
    """
    base = Path(filename).name
    if not base.endswith(".sol"):
        raise UploadRejectedError("file must be a .sol contract")
    stem = base[: -len(".sol")]
    if not _SAFE_STEM.match(stem):
        raise UploadRejectedError(
            "contract name must be alphanumeric/underscore and start with a letter"
        )
    return stem


def save_and_compile(
    filename: str,
    source: bytes,
    *,
    sandbox_dir: Path,
    contracts_subdir: str = "contracts/uploads",
) -> UploadedContract:
    """Write an uploaded contract into the sandbox and compile it.

    Uploads land in a gitignored ``contracts/uploads/`` subdir — kept separate
    from the committed demo contracts, but still under the Foundry ``src`` so
    ``forge build`` (which compiles ``contracts`` recursively) picks them up.

    Args:
        filename: The uploaded filename; its stem becomes the source filename.
        source: The raw Solidity source bytes.
        sandbox_dir: The Foundry project root (holds ``foundry.toml``).
        contracts_subdir: Directory under ``sandbox_dir`` to write the source to.

    Returns:
        The :class:`UploadedContract` ready for deployment.

    Raises:
        UploadRejectedError: If the filename/source is invalid.
        CompileError: If ``forge build`` reports a compile error.
    """
    stem = safe_stem(filename)
    if not source.strip():
        raise UploadRejectedError("contract source is empty")
    dest = sandbox_dir / contracts_subdir / f"{stem}.sol"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source)
    _forge_build(sandbox_dir)
    contract = _resolve_contract(sandbox_dir / "out" / f"{stem}.sol", stem)
    repo_root = sandbox_dir.parent
    rel = dest.relative_to(repo_root).as_posix()
    return UploadedContract(path=rel, contract=contract, label=stem)


def constructor_is_nullary(contract: str, artifacts_dir: Path) -> bool:
    """Return True if the compiled contract has a no-argument constructor."""
    ctor = next(
        (e for e in _abi(contract, artifacts_dir) if e.get("type") == "constructor"),
        None,
    )
    return ctor is None or not ctor.get("inputs")


def nullary_functions(
    contract: str, artifacts_dir: Path, *, limit: int = 6
) -> list[str]:
    """Return names of state-changing functions that take no arguments.

    These are the only functions we can call generically (no argument synthesis),
    so they're what the plumbing run measures gas for.
    """
    out: list[str] = []
    for entry in _abi(contract, artifacts_dir):
        if entry.get("type") != "function" or entry.get("inputs"):
            continue
        if entry.get("stateMutability") in {"view", "pure"}:
            continue
        out.append(str(entry["name"]))
        if len(out) >= limit:
            break
    return out


def _forge_build(sandbox_dir: Path) -> None:
    """Run ``forge build`` in the sandbox, raising CompileError on error."""
    try:
        proc = subprocess.run(
            ["forge", "build"],
            cwd=sandbox_dir,
            capture_output=True,
            text=True,
            timeout=_FORGE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:  # forge not installed
        raise CompileError("forge is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise CompileError("compile timed out") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "compile error").strip()
        raise CompileError(detail[-800:])


def _resolve_contract(artifact_dir: Path, stem: str) -> str:
    """Pick the deployable contract name from a compiled file's artifacts.

    Foundry writes ``out/<File>.sol/<Name>.json`` per contract in the file. Prefer
    the artifact matching the filename stem; otherwise take the last one defined.
    """
    if not artifact_dir.is_dir():
        raise CompileError("no artifact produced (does the file define a contract?)")
    names = sorted(p.stem for p in artifact_dir.glob("*.json"))
    if not names:
        raise CompileError("no contract artifact produced")
    return stem if stem in names else names[-1]


def _abi(contract: str, artifacts_dir: Path) -> list[dict[str, Any]]:
    """Load a compiled contract's ABI from ``out/<contract>.sol/<contract>.json``."""
    path = artifacts_dir / f"{contract}.sol" / f"{contract}.json"
    if not path.is_file():
        # Artifact filename may differ from the contract name; search for it.
        matches = list(artifacts_dir.glob(f"*/{contract}.json"))
        if not matches:
            return []
        path = matches[0]
    data = json.loads(path.read_text())
    abi = data.get("abi", [])
    return abi if isinstance(abi, list) else []
