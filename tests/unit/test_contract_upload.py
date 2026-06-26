from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from demo.contract_upload import _forge_build, save_and_compile

from sentinel.web import CompileError


def test_upload_compile_targets_only_the_new_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> object:
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    _forge_build(tmp_path, "contracts/audit_workdir/Coinbox.sol")

    assert calls == [["forge", "build", "contracts/audit_workdir/Coinbox.sol"]]


def test_failed_upload_compile_removes_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(args: list[str], **_kwargs: Any) -> object:
        return SimpleNamespace(returncode=1, stdout="", stderr="compile failed")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CompileError):
        save_and_compile("Broken.sol", b"contract Broken {", sandbox_dir=tmp_path)

    assert not (tmp_path / "contracts" / "audit_workdir" / "Broken.sol").exists()
