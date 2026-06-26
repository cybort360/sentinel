from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sentinel.mcp_servers.codebase_mcp.config import (
    CodebaseConfig,
    CodebaseDegradedError,
)
from sentinel.mcp_servers.codebase_mcp.engine import CodebaseEngine


def test_read_contract_resolves_bare_solidity_filename(tmp_path: Path) -> None:
    contracts = tmp_path / "sandbox" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "Vault.sol").write_text("pragma solidity ^0.8.20; contract Vault {}")
    engine = CodebaseEngine(CodebaseConfig(repo_root=tmp_path))

    result = engine.read_contract("Vault.sol")

    assert result.path == "sandbox/contracts/Vault.sol"
    assert result.summary.contracts == ["Vault"]


def test_read_contract_resolves_staged_patch_alias(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "sentinel@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "SENTINEL"],
        cwd=tmp_path,
        check=True,
    )
    contracts = tmp_path / "sandbox" / "contracts"
    contracts.mkdir(parents=True)
    target = contracts / "Vault.sol"
    target.write_text(
        "pragma solidity ^0.8.20; contract Vault { function a() external {} }"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)
    engine = CodebaseEngine(CodebaseConfig(repo_root=tmp_path))

    patch = engine.propose_patch(
        "sandbox/contracts/Vault.sol",
        "pragma solidity ^0.8.20; contract Vault { function b() external {} }",
    )
    staged = engine.read_contract(f"contracts/staged_patches/{patch.patch_id}.sol")

    assert "function b()" in staged.source
    assert "function a()" in target.read_text()


def test_uploaded_staged_patch_builds_artifact_before_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git(tmp_path)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "foundry.toml").write_text("[profile.default]\nsrc = 'contracts'\n")
    rel = "sandbox/contracts/audit_workdir/Vault.sol"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_text("pragma solidity ^0.8.20; contract Vault {}")
    calls: list[list[str]] = []

    def fake_build(
        self: CodebaseEngine, build_root: Path, staged_rel: Path
    ) -> SimpleNamespace:
        assert isinstance(self, CodebaseEngine)
        calls.append(["forge", "build", staged_rel.as_posix()])
        assert build_root == sandbox
        artifact = sandbox / "out" / f"{staged_rel.stem}.sol" / "Vault.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("{}")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(CodebaseEngine, "_run_forge_build", fake_build)
    engine = CodebaseEngine(CodebaseConfig(repo_root=tmp_path))

    result = engine.propose_patch(
        rel,
        "pragma solidity ^0.8.20; contract Vault { function patched() external {} }",
    )

    assert calls == [
        [
            "forge",
            "build",
            f"contracts/staged_patches/{result.patch_id}.sol",
        ]
    ]
    assert result.staged_source_path == (
        f"sandbox/contracts/staged_patches/{result.patch_id}.sol"
    )
    assert result.staged_artifact == f"{result.staged_source_path}:Vault"
    assert result.contract_name == "Vault"
    assert result.original_target_path == rel
    assert result.artifact_path == f"sandbox/out/{result.patch_id}.sol/Vault.json"
    assert engine.staged_artifact(result.patch_id) == result.staged_artifact
    metadata = engine.staged_metadata(result.patch_id)
    assert metadata is not None
    assert metadata.deploy_target == result.staged_artifact
    assert metadata.artifact_path == result.artifact_path


def test_uploaded_staged_patch_missing_artifact_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git(tmp_path)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "foundry.toml").write_text("[profile.default]\nsrc = 'contracts'\n")
    rel = "sandbox/contracts/audit_workdir/Vault.sol"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_text("pragma solidity ^0.8.20; contract Vault {}")

    def fake_build(
        self: CodebaseEngine, build_root: Path, staged_rel: Path
    ) -> SimpleNamespace:
        assert isinstance(self, CodebaseEngine)
        assert build_root == sandbox
        assert staged_rel.name.endswith(".sol")
        assert staged_rel.name != "Vault.sol"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(CodebaseEngine, "_run_forge_build", fake_build)
    engine = CodebaseEngine(CodebaseConfig(repo_root=tmp_path))

    with pytest.raises(CodebaseDegradedError, match="patch_artifact_missing") as exc:
        engine.propose_patch(
            rel,
            "pragma solidity ^0.8.20; contract Vault { "
            "function patched() external {} }",
        )
    assert "contract Vault" in str(exc.value)
    assert "contracts/staged_patches/" in str(exc.value)


def test_uploaded_staged_patch_compile_failure_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_git(tmp_path)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "foundry.toml").write_text("[profile.default]\nsrc = 'contracts'\n")
    rel = "sandbox/contracts/audit_workdir/Vault.sol"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_text("pragma solidity ^0.8.20; contract Vault {}")

    def fake_build(
        self: CodebaseEngine, build_root: Path, staged_rel: Path
    ) -> SimpleNamespace:
        assert isinstance(self, CodebaseEngine)
        assert build_root == sandbox
        assert staged_rel.name.endswith(".sol")
        assert staged_rel.name != "Vault.sol"
        return SimpleNamespace(returncode=1, stdout="", stderr="ParserError")

    monkeypatch.setattr(CodebaseEngine, "_run_forge_build", fake_build)
    engine = CodebaseEngine(CodebaseConfig(repo_root=tmp_path))

    with pytest.raises(CodebaseDegradedError, match="patch_compile_failed"):
        engine.propose_patch(
            rel,
            "pragma solidity ^0.8.20; contract Vault { "
            "function patched() external {} }",
        )


def _init_git(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "sentinel@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "SENTINEL"],
        cwd=path,
        check=True,
    )
    (path / "README.md").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True)
