"""Configuration and errors for CodebaseMCP (architecture.md §5.1).

CodebaseMCP reads/indexes contracts and stages patches as git branches. It never
edits the canonical file directly — staging happens on a branch and only
``apply_patch`` (gated by the Human Checkpoint, §7) brings a patch into the
canonical tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]


class CodebaseDegradedError(RuntimeError):
    """Raised when a CodebaseMCP filesystem/git dependency is unavailable.

    Always paired with a structured ``[DEGRADED]`` log entry (Rule 4). Note that
    a *parse* failure is NOT degraded — it is handled in-band by flagging the
    affected functions ``unauditable`` and continuing.
    """


@dataclass(frozen=True)
class CodebaseConfig:
    """Settings for a CodebaseMCP engine.

    Attributes:
        repo_root: The git working tree that holds the canonical contracts.
            Read/parse tools accept any path; patch tools operate within this
            repo.
    """

    repo_root: Path = _REPO_ROOT

    @classmethod
    def from_env(cls) -> CodebaseConfig:
        """Build a config from environment variables (``CODEBASE_REPO_ROOT``)."""
        root = os.environ.get("CODEBASE_REPO_ROOT")
        return cls(repo_root=Path(root) if root else _REPO_ROOT)
