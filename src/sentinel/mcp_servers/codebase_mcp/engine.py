"""CodebaseMCP engine: read, index, and stage patches for the target protocol.

The testable core behind the FastMCP server (architecture.md §5.1). Reading and
parsing tolerate malformed input (Rule 4 — functions flagged ``unauditable``,
never a crash). Patching is git-branch staged and only ``apply_patch`` — which
the orchestrator calls **after** the Human Checkpoint approves (Rule 2) — brings
a patch into the canonical file.
"""

from __future__ import annotations

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
)
from sentinel.observability.trace_logger import get_logger


@dataclass(frozen=True)
class _PatchRecord:
    """Bookkeeping for a staged patch."""

    patch_id: str
    rel_path: str
    branch: str
    base_commit: str


class CodebaseEngine:
    """Reads contracts and stages/apply git-branch patches."""

    def __init__(self, config: CodebaseConfig) -> None:
        """Initialise against a repo root (git only required for patch tools)."""
        self._config = config
        self._log = get_logger("codebase_mcp")
        self._patches: dict[str, _PatchRecord] = {}
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
        resolved = self._resolve(path)
        source = self._read_text(resolved)
        summary = parse_source(path, source)
        if summary.unauditable_functions:
            self._log.warning(
                "[DEGRADED] contract has unauditable functions",
                path=path,
                unauditable=summary.unauditable_functions,
            )
        self._log.info(
            "read_contract",
            path=path,
            functions=len(summary.functions),
            unauditable=len(summary.unauditable_functions),
        )
        return ReadResult(path=path, source=source, summary=summary)

    def list_functions(self, path: str) -> ListResult:
        """List function signatures with visibility/mutability for a contract.

        Args:
            path: Contract path (absolute, or relative to ``repo_root``).

        Returns:
            A :class:`ListResult`; unparseable functions are still listed and
            flagged ``unauditable`` (Rule 4).
        """
        resolved = self._resolve(path)
        summary = parse_source(path, self._read_text(resolved))
        return ListResult(
            path=path,
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
        rel_path = self._rel(path)
        patch_id = uuid4().hex
        branch = f"sentinel/patch-{patch_id[:8]}"
        base_commit = self._workspace().stage_patch(
            rel_path, new_content, branch, f"propose patch {patch_id[:8]} on {rel_path}"
        )
        self._patches[patch_id] = _PatchRecord(patch_id, rel_path, branch, base_commit)
        self._log.info("propose_patch", patch_id=patch_id, path=rel_path, branch=branch)
        return ProposeResult(
            patch_id=patch_id, path=rel_path, branch=branch, base_commit=base_commit
        )

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

    # -- internals ------------------------------------------------------------

    def _workspace(self) -> GitWorkspace:
        """Lazily construct (and cache) the git workspace."""
        if self._git is None:
            self._git = GitWorkspace(self._config.repo_root)
        return self._git

    def _resolve(self, path: str) -> Path:
        """Resolve a path to absolute (relative paths are under ``repo_root``)."""
        p = Path(path)
        return p if p.is_absolute() else self._config.repo_root / p

    def _rel(self, path: str) -> str:
        """Return a repo-root-relative path string for git operations."""
        p = Path(path)
        if p.is_absolute():
            return str(p.relative_to(self._config.repo_root))
        return str(p)

    def _read_text(self, resolved: Path) -> str:
        """Read a file, degrading gracefully if it is missing (Rule 4)."""
        try:
            return resolved.read_text()
        except OSError as exc:
            self._log.error(
                "[DEGRADED] cannot read contract", path=str(resolved), error=str(exc)
            )
            raise CodebaseDegradedError(f"cannot read {resolved}: {exc}") from exc

    def _patch(self, patch_id: str) -> _PatchRecord:
        """Look up a staged patch record, degrading if unknown (Rule 4)."""
        record = self._patches.get(patch_id)
        if record is None:
            self._log.error("[DEGRADED] unknown patch_id", patch_id=patch_id)
            raise CodebaseDegradedError(f"unknown patch_id {patch_id!r}")
        return record
