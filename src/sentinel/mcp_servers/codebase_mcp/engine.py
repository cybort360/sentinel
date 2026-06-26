"""CodebaseMCP engine: read, index, and stage patches for the target protocol.

The testable core behind the FastMCP server (architecture.md §5.1). Reading and
parsing tolerate malformed input (Rule 4 — functions flagged ``unauditable``,
never a crash). Patching is git-branch staged and only ``apply_patch`` — which
the orchestrator calls **after** the Human Checkpoint approves (Rule 2) — brings
a patch into the canonical file.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sentinel.mcp_servers.codebase_mcp.config import (
    CodebaseConfig,
    CodebaseDegradedError,
)
from sentinel.mcp_servers.codebase_mcp.git_workspace import GitWorkspace
from sentinel.mcp_servers.codebase_mcp.parser import parse_source
from sentinel.mcp_servers.codebase_mcp.results import (
    ApplyResult,
    DiffResult,
    ListResult,
    ProposeResult,
    ReadResult,
    StagedPatchMetadata,
)
from sentinel.observability.trace_logger import get_logger


@dataclass(frozen=True)
class _PatchRecord:
    """Bookkeeping for a staged patch."""

    patch_id: str
    rel_path: str
    branch: str
    base_commit: str
    staged_source_path: str | None = None
    staged_artifact: str | None = None
    contract_name: str | None = None
    artifact_path: str | None = None
    original_target_path: str | None = None


class CodebaseEngine:
    """Reads contracts and stages/apply git-branch patches."""

    def __init__(self, config: CodebaseConfig) -> None:
        """Initialise against a repo root (git only required for patch tools)."""
        self._config = config
        self._log = get_logger("codebase_mcp")
        self._patches: dict[str, _PatchRecord] = {}
        self._latest_patch_id: str | None = None
        self._last_patch_error: str | None = None
        self._git: GitWorkspace | None = None

    # -- read / index ---------------------------------------------------------

    def read_contract(self, path: str) -> ReadResult:
        """Read a contract's source and parsed surface summary.

        Args:
            path: Contract path (absolute, or relative to ``repo_root``).

        Returns:
            A :class:`ReadResult` with source and a :class:`ContractSummary`.

        Raises:
            CodebaseDegradedError: If the file cannot be read.
        """
        rel_path, source = self._source_for_read(path)
        summary = parse_source(rel_path, source)
        if summary.unauditable_functions:
            self._log.warning(
                "[DEGRADED] contract has unauditable functions",
                path=rel_path,
                unauditable=summary.unauditable_functions,
            )
        self._log.info(
            "read_contract",
            path=rel_path,
            functions=len(summary.functions),
            unauditable=len(summary.unauditable_functions),
        )
        return ReadResult(path=rel_path, source=source, summary=summary)

    def list_functions(self, path: str) -> ListResult:
        """List function signatures with visibility/mutability for a contract.

        Args:
            path: Contract path (absolute, or relative to ``repo_root``).

        Returns:
            A :class:`ListResult`; unparseable functions are still listed and
            flagged ``unauditable`` (Rule 4).
        """
        rel_path, source = self._source_for_read(path)
        summary = parse_source(rel_path, source)
        return ListResult(
            path=rel_path,
            functions=summary.functions,
            unauditable_functions=summary.unauditable_functions,
        )

    # -- patch staging --------------------------------------------------------

    def propose_patch(self, path: str, new_content: str) -> ProposeResult:
        """Stage a patch on a new git branch — the canonical file is untouched.

        Args:
            path: Contract path relative to ``repo_root``.
            new_content: The full proposed source for the file.

        Returns:
            A :class:`ProposeResult` with the ``patch_id`` and staging branch.
        """
        self._last_patch_error = None
        try:
            rel_path = self._rel(path)
            patch_id = uuid4().hex
            branch = f"sentinel/patch-{patch_id[:8]}"
            base_commit = self._workspace().stage_patch(
                rel_path,
                new_content,
                branch,
                f"propose patch {patch_id[:8]} on {rel_path}",
            )
            (
                staged_source_path,
                staged_artifact,
                contract_name,
                artifact_path,
            ) = self._prepare_staged_artifact(patch_id, rel_path, new_content)
            self._patches[patch_id] = _PatchRecord(
                patch_id,
                rel_path,
                branch,
                base_commit,
                staged_source_path,
                staged_artifact,
                contract_name,
                artifact_path,
                rel_path,
            )
            self._latest_patch_id = patch_id
            self._log.info(
                "propose_patch",
                patch_id=patch_id,
                path=rel_path,
                branch=branch,
                staged_artifact=staged_artifact,
                contract_name=contract_name,
                artifact_path=artifact_path,
            )
            return ProposeResult(
                patch_id=patch_id,
                path=rel_path,
                branch=branch,
                base_commit=base_commit,
                staged_source_path=staged_source_path,
                staged_artifact=staged_artifact,
                contract_name=contract_name,
                artifact_path=artifact_path,
                original_target_path=rel_path,
            )
        except Exception as exc:
            self._last_patch_error = str(exc)
            self._log.error("[DEGRADED] propose_patch failed", error=str(exc))
            raise

    def diff_patch(self, patch_id: str) -> DiffResult:
        """Return the unified diff for a staged patch (for human review).

        Args:
            patch_id: The id returned by ``propose_patch``.

        Returns:
            A :class:`DiffResult` with the unified diff.
        """
        record = self._patch(patch_id)
        diff = self._workspace().diff(
            record.base_commit, record.branch, record.rel_path
        )
        return DiffResult(patch_id=patch_id, path=record.rel_path, diff=diff)

    def apply_patch(self, patch_id: str) -> ApplyResult:
        """Apply a staged patch to the canonical file.

        Gated by the Human Checkpoint (architecture.md §7, CLAUDE.md Rule 2):
        the orchestrator must only call this after an approval. CodebaseMCP is
        the mechanism, not the gate.

        Args:
            patch_id: The id returned by ``propose_patch``.

        Returns:
            An :class:`ApplyResult` with the new canonical commit.
        """
        record = self._patch(patch_id)
        commit = self._workspace().apply_from_branch(
            record.branch,
            record.rel_path,
            f"apply patch {patch_id[:8]} on {record.rel_path}",
        )
        self._log.info(
            "apply_patch", patch_id=patch_id, path=record.rel_path, commit=commit
        )
        return ApplyResult(
            patch_id=patch_id, path=record.rel_path, applied=True, commit=commit
        )

    def staged_artifact(self, patch_id: str) -> str | None:
        """Return the compiled staged artifact target for a patch, if prepared."""
        return self._patch(patch_id).staged_artifact

    def staged_metadata(self, patch_id: str) -> StagedPatchMetadata | None:
        """Return compiled staged-patch metadata for dynamic verification."""
        record = self._patch(patch_id)
        if not (
            record.staged_source_path
            and record.staged_artifact
            and record.contract_name
            and record.artifact_path
            and record.original_target_path
        ):
            return None
        return StagedPatchMetadata(
            patch_id=record.patch_id,
            staged_source_path=record.staged_source_path,
            contract_name=record.contract_name,
            artifact_path=record.artifact_path,
            original_target_path=record.original_target_path,
            deploy_target=record.staged_artifact,
        )

    def latest_patch(self) -> ProposeResult | None:
        """Return the most recent successful staged patch, if any."""
        if self._latest_patch_id is None:
            return None
        record = self._patch(self._latest_patch_id)
        return ProposeResult(
            patch_id=record.patch_id,
            path=record.rel_path,
            branch=record.branch,
            base_commit=record.base_commit,
            staged_source_path=record.staged_source_path,
            staged_artifact=record.staged_artifact,
            contract_name=record.contract_name,
            artifact_path=record.artifact_path,
            original_target_path=record.original_target_path,
        )

    def latest_patch_error(self) -> str | None:
        """Return the most recent propose_patch failure detail, if any."""
        return self._last_patch_error

    # -- internals ------------------------------------------------------------

    def _workspace(self) -> GitWorkspace:
        """Lazily construct (and cache) the git workspace."""
        if self._git is None:
            self._git = GitWorkspace(self._config.repo_root)
        return self._git

    def _source_for_read(self, path: str) -> tuple[str, str]:
        """Return ``(rel_path, source)`` for canonical or staged-patch aliases."""
        staged = self._staged_alias(path)
        if staged is not None:
            record = staged
            alias = f"contracts/staged_patches/{record.patch_id}.sol"
            return alias, self._workspace().read_from_branch(
                record.branch, record.rel_path
            )
        resolved = self._resolve(path)
        return self._rel_from_resolved(resolved), self._read_text(resolved)

    def _staged_alias(self, path: str) -> _PatchRecord | None:
        """Resolve ``contracts/staged_patches/<patch_id>.sol`` to a patch record."""
        p = Path(path)
        parts = p.parts
        if len(parts) == 3 and parts[:2] == ("contracts", "staged_patches"):
            return self._patches.get(p.stem)
        return None

    def _resolve(self, path: str) -> Path:
        """Resolve a path to absolute (relative paths are under ``repo_root``)."""
        p = Path(path)
        if p.is_absolute():
            return p
        direct = self._config.repo_root / p
        if direct.exists() or p.parent != Path(".") or p.suffix != ".sol":
            return direct
        sandbox_contract = self._config.repo_root / "sandbox" / "contracts" / p.name
        return sandbox_contract if sandbox_contract.exists() else direct

    def _rel(self, path: str) -> str:
        """Return a repo-root-relative path string for git operations."""
        return self._rel_from_resolved(self._resolve(path))

    def _rel_from_resolved(self, path: Path) -> str:
        """Return a repo-root-relative path for an already resolved path."""
        return path.relative_to(self._config.repo_root).as_posix()

    def _read_text(self, resolved: Path) -> str:
        """Read a file, degrading gracefully if it is missing (Rule 4)."""
        try:
            return resolved.read_text()
        except OSError as exc:
            self._log.error(
                "[DEGRADED] cannot read contract", path=str(resolved), error=str(exc)
            )
            raise CodebaseDegradedError(f"cannot read {resolved}: {exc}") from exc

    def _prepare_staged_artifact(
        self, patch_id: str, rel_path: str, new_content: str
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Compile uploaded staged patches so SimulationMCP has an artifact."""
        if not self._should_build_staged_artifact(rel_path):
            return None, None, None, None
        contract_name = self._contract_name(rel_path, new_content)
        sandbox = self._config.repo_root / "sandbox"
        staged_rel = Path("contracts") / "staged_patches" / f"{patch_id}.sol"
        staged_abs = sandbox / staged_rel
        staged_abs.parent.mkdir(parents=True, exist_ok=True)
        staged_abs.write_text(new_content)

        proc = self._run_forge_build(sandbox, staged_rel)
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "forge build failed").strip()
            self._log.error(
                "[DEGRADED] patch_compile_failed",
                patch_id=patch_id,
                path=rel_path,
                stderr=message,
            )
            raise CodebaseDegradedError(f"patch_compile_failed: {message}")

        artifact = sandbox / "out" / f"{patch_id}.sol" / f"{contract_name}.json"
        if not artifact.is_file():
            self._log.error(
                "[DEGRADED] patch_artifact_missing",
                patch_id=patch_id,
                path=rel_path,
                source=staged_rel.as_posix(),
                contract_name=contract_name,
                artifact=str(artifact),
            )
            raise CodebaseDegradedError(
                "patch_artifact_missing: compiled staged patch source "
                f"{staged_rel.as_posix()} for contract {contract_name}, but no "
                f"artifact exists at {artifact}"
            )

        staged_source_path = f"sandbox/{staged_rel.as_posix()}"
        staged_artifact = f"{staged_source_path}:{contract_name}"
        artifact_path = artifact.relative_to(self._config.repo_root).as_posix()
        self._log.info(
            "build_staged_patch",
            patch_id=patch_id,
            source=staged_source_path,
            contract_name=contract_name,
            artifact=staged_artifact,
            artifact_path=artifact_path,
            original_target_path=rel_path,
        )
        return staged_source_path, staged_artifact, contract_name, artifact_path

    def _run_forge_build(
        self, sandbox: Path, staged_rel: Path
    ) -> subprocess.CompletedProcess[str]:
        """Run Foundry build for a staged source."""
        return subprocess.run(
            ["forge", "build", staged_rel.as_posix()],
            cwd=sandbox,
            capture_output=True,
            text=True,
            check=False,
        )

    def _should_build_staged_artifact(self, rel_path: str) -> bool:
        """Build only browser-uploaded audit contracts in repos with Foundry."""
        return (
            rel_path.startswith("sandbox/contracts/audit_workdir/")
            or rel_path.startswith("sandbox/contracts/uploads/")
        ) and (self._config.repo_root / "sandbox" / "foundry.toml").is_file()

    def _contract_name(self, rel_path: str, source: str) -> str:
        """Select the Solidity contract name for the staged artifact target."""
        summary = parse_source(rel_path, source)
        stem = Path(rel_path).stem
        if stem in summary.contracts:
            return stem
        if summary.contracts:
            return summary.contracts[-1]
        self._log.error("[DEGRADED] patch_artifact_missing", path=rel_path)
        raise CodebaseDegradedError(
            "patch_artifact_missing: staged source has no contract declaration"
        )

    def _patch(self, patch_id: str) -> _PatchRecord:
        """Look up a staged patch record, degrading if unknown (Rule 4)."""
        record = self._patches.get(patch_id)
        if record is None:
            self._log.error("[DEGRADED] unknown patch_id", patch_id=patch_id)
            raise CodebaseDegradedError(f"unknown patch_id {patch_id!r}")
        return record
