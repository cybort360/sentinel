"""Git-backed patch staging for CodebaseMCP (architecture.md §5.1).

Patches are staged on dedicated branches and never touch the canonical file
until ``apply_patch``. This wraps ``git`` over subprocess (no GitPython dep) and
keeps the working tree on the canonical branch between operations: a proposed
patch lives only on its branch until a human approves applying it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from sentinel.mcp_servers.codebase_mcp.config import CodebaseDegradedError
from sentinel.observability.trace_logger import get_logger

_log = get_logger("codebase_mcp.git")


class GitWorkspace:
    """A thin git wrapper rooted at a working tree."""

    def __init__(self, repo_root: Path) -> None:
        """Validate that ``repo_root`` is inside a git work tree.

        Raises:
            CodebaseDegradedError: If ``repo_root`` is not a git repository.
        """
        self.repo_root = repo_root
        try:
            inside = self._git("rev-parse", "--is-inside-work-tree")
        except CodebaseDegradedError:
            raise
        if inside.strip() != "true":
            raise CodebaseDegradedError(f"{repo_root} is not a git work tree")

    def _git(self, *args: str) -> str:
        """Run a git command in the repo, returning stdout (Rule 4 on failure)."""
        proc = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            _log.error(
                "[DEGRADED] git failed", args=list(args), stderr=proc.stderr.strip()
            )
            raise CodebaseDegradedError(
                f"git {' '.join(args)} failed: {proc.stderr.strip()}"
            )
        return proc.stdout

    def _git_ok(self, *args: str) -> bool:
        """Return whether a git command succeeds, without logging degradation."""
        proc = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0

    def current_branch(self) -> str:
        """Return the current branch name (or ``HEAD`` if detached)."""
        return self._git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def head_commit(self) -> str:
        """Return the current HEAD commit sha."""
        return self._git("rev-parse", "HEAD").strip()

    def stage_patch(
        self, rel_path: str, new_content: str, branch: str, message: str
    ) -> str:
        """Commit ``new_content`` for ``rel_path`` on a new ``branch``.

        The working tree is restored to the original branch afterwards, so the
        canonical file is left untouched.

        Returns:
            The base commit sha the branch was created from.
        """
        base_branch = self.current_branch()
        base_commit = self.head_commit()
        restore = base_branch if base_branch != "HEAD" else base_commit
        target = self.repo_root / rel_path
        existed_before = target.exists()
        original = target.read_text() if existed_before else None
        tracked_before = self._git_ok("ls-files", "--error-unmatch", rel_path)
        self._git("checkout", "-b", branch, base_commit)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_content)
            self._git("add", "--", rel_path)
            self._git("commit", "-m", message)
        finally:
            self._git("checkout", restore)
            if not tracked_before:
                if existed_before and original is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(original)
                else:
                    target.unlink(missing_ok=True)
        return base_commit

    def diff(self, base_commit: str, branch: str, rel_path: str) -> str:
        """Return the unified diff of ``rel_path`` between base and branch."""
        return self._git("diff", base_commit, branch, "--", rel_path)

    def read_from_branch(self, branch: str, rel_path: str) -> str:
        """Read ``rel_path`` from ``branch`` without checking it out."""
        return self._git("show", f"{branch}:{rel_path}")

    def apply_from_branch(self, branch: str, rel_path: str, message: str) -> str:
        """Bring ``rel_path`` from ``branch`` into the canonical tree and commit.

        Returns:
            The new HEAD commit sha on the canonical branch.
        """
        self._git("checkout", branch, "--", rel_path)
        self._git("add", "--", rel_path)
        self._git("commit", "-m", message)
        return self.head_commit()
