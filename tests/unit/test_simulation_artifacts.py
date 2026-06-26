from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.mcp_servers.simulation_mcp.artifacts import load_artifact


@pytest.mark.parametrize(
    "identifier",
    [
        "SubscriptionBilling",
        "SubscriptionBilling.sol",
        "contracts/SubscriptionBilling.sol",
        "sandbox/contracts/SubscriptionBilling.sol",
        "contracts/SubscriptionBilling.sol:SubscriptionBilling",
    ],
)
def test_load_artifact_accepts_name_or_source_path(
    tmp_path: Path, identifier: str
) -> None:
    artifact_dir = tmp_path / "SubscriptionBilling.sol"
    artifact_dir.mkdir()
    (artifact_dir / "SubscriptionBilling.json").write_text(
        json.dumps({"abi": [], "bytecode": {"object": "0x6000"}})
    )

    artifact = load_artifact(identifier, tmp_path)

    assert artifact.name == "SubscriptionBilling"
    assert artifact.bytecode == "0x6000"


def test_load_artifact_finds_contract_when_source_name_differs(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "patch123.sol"
    artifact_dir.mkdir()
    (artifact_dir / "VulnerableSubscriptionVault.json").write_text(
        json.dumps({"abi": [], "bytecode": {"object": "0x6001"}})
    )

    artifact = load_artifact("contracts/staged_patches/patch123.sol", tmp_path)

    assert artifact.name == "VulnerableSubscriptionVault"
    assert artifact.bytecode == "0x6001"
