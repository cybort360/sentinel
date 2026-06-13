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
