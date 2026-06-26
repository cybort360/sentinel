"""Integration tests for CodebaseMCP's git-backed patch flow.

These exercise real ``git`` against a temporary repo seeded with a sandbox
contract: propose stages on a branch (canonical file untouched), diff shows the
patch, apply brings it into the canonical file. Marked integration because they
shell out to git and mutate a real (temp) repo.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from sentinel.mcp_servers.codebase_mcp.config import CodebaseConfig
from sentinel.mcp_servers.codebase_mcp.engine import CodebaseEngine

pytestmark = pytest.mark.integration

_SANDBOX = Path(__file__).resolve().parents[2] / "sandbox" / "contracts"
_REL = "contracts/SubscriptionBilling.sol"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[CodebaseEngine]:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    (tmp_path / "contracts").mkdir()
    shutil.copy(_SANDBOX / "SubscriptionBilling.sol", tmp_path / _REL)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True
        )

    git("init", "-b", "main")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-m", "seed")
    yield CodebaseEngine(CodebaseConfig(repo_root=tmp_path))


def _guarded_source() -> str:
    # Use the real guarded variant as the proposed patch content.
    return (_SANDBOX / "SubscriptionBillingGuarded.sol").read_text()


def test_propose_does_not_touch_canonical_file(engine: CodebaseEngine) -> None:
    canonical = engine._config.repo_root / _REL
    before = canonical.read_text()

    result = engine.propose_patch(_REL, _guarded_source())

    assert result.patch_id
    assert result.branch.startswith("sentinel/patch-")
    # Canonical file is unchanged; the patch lives only on its branch.
    assert canonical.read_text() == before


def test_diff_shows_the_reentrancy_guard(engine: CodebaseEngine) -> None:
    result = engine.propose_patch(_REL, _guarded_source())
    diff = engine.diff_patch(result.patch_id).diff
    assert "nonReentrant" in diff
    assert diff.lstrip().startswith("diff --git")


def test_apply_updates_canonical_file(engine: CodebaseEngine) -> None:
    canonical = engine._config.repo_root / _REL
    result = engine.propose_patch(_REL, _guarded_source())
    assert "nonReentrant" not in canonical.read_text()  # not yet applied

    applied = engine.apply_patch(result.patch_id)

    assert applied.applied
    assert applied.commit
    assert "nonReentrant" in canonical.read_text()  # now applied


def test_read_and_list_work_on_the_repo(engine: CodebaseEngine) -> None:
    read = engine.read_contract(_REL)
    assert "SubscriptionBilling" in read.summary.contracts
    listing = engine.list_functions(_REL)
    assert any(f.name == "cancelSubscription" for f in listing.functions)


def test_uploaded_contract_patch_staging_produces_diff(
    engine: CodebaseEngine,
) -> None:
    rel = "sandbox/contracts/audit_workdir/VulnerableSubscriptionVault.sol"
    source = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract VulnerableSubscriptionVault {
    mapping(address => uint256) public balances;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    function withdraw() external {
        uint256 amount = balances[msg.sender];
        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok, "send failed");
        balances[msg.sender] = 0;
    }
}
"""
    fixed = source.replace(
        '(bool ok, ) = msg.sender.call{value: amount}("");\n'
        '        require(ok, "send failed");\n'
        "        balances[msg.sender] = 0;",
        "balances[msg.sender] = 0;\n"
        '        (bool ok, ) = msg.sender.call{value: amount}("");\n'
        '        require(ok, "send failed");',
    )
    path = engine._config.repo_root / rel
    path.parent.mkdir(parents=True)
    path.write_text(source)

    result = engine.propose_patch(rel, fixed)
    diff = engine.diff_patch(result.patch_id).diff

    assert result.patch_id
    assert diff.strip()
    assert "balances[msg.sender] = 0;" in diff
    assert path.read_text() == source


def test_uploaded_staged_patch_compiles_and_records_artifact(tmp_path: Path) -> None:
    if shutil.which("git") is None or shutil.which("forge") is None:
        pytest.skip("git/forge not installed")
    _init_git_repo(tmp_path)
    sandbox = tmp_path / "sandbox"
    (sandbox / "contracts" / "audit_workdir").mkdir(parents=True)
    (sandbox / "foundry.toml").write_text("[profile.default]\nsrc = 'contracts'\n")
    rel = "sandbox/contracts/audit_workdir/VulnerableSubscriptionVault.sol"
    original = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract VulnerableSubscriptionVault {
    uint256 public total;

    function deposit() external payable {
        total += msg.value;
    }
}
"""
    patched = original.replace(
        "uint256 public total;",
        "uint256 public total;\n    address public owner;",
    )
    target = tmp_path / rel
    target.write_text(original)

    engine = CodebaseEngine(CodebaseConfig(repo_root=tmp_path))
    result = engine.propose_patch(rel, patched)

    assert result.staged_source_path == (
        f"sandbox/contracts/staged_patches/{result.patch_id}.sol"
    )
    assert result.contract_name == "VulnerableSubscriptionVault"
    assert result.artifact_path == (
        f"sandbox/out/{result.patch_id}.sol/VulnerableSubscriptionVault.json"
    )
    assert result.staged_artifact == (
        f"{result.staged_source_path}:VulnerableSubscriptionVault"
    )
    assert (tmp_path / result.artifact_path).is_file()
    assert "address public owner;" in (tmp_path / result.staged_source_path).read_text()


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("seed")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=path, check=True)
