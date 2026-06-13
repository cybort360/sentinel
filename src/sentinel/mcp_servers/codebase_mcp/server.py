"""FastMCP server exposing the five CodebaseMCP tools (architecture.md §5.1).

Thin wiring over :class:`CodebaseEngine`; logic and tests live there. Tools are
registered with :meth:`FastMCP.add_tool` so the underlying functions keep their
type annotations under ``mypy --strict``. The docstrings are the tool
descriptions the model sees.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from sentinel.mcp_servers.codebase_mcp.config import CodebaseConfig
from sentinel.mcp_servers.codebase_mcp.engine import CodebaseEngine
from sentinel.mcp_servers.runtime import run_server

_engine: CodebaseEngine | None = None


def _get_engine() -> CodebaseEngine:
    """Return the lazily-initialised CodebaseMCP engine."""
    global _engine
    if _engine is None:
        _engine = CodebaseEngine(CodebaseConfig.from_env())
    return _engine


def read_contract(path: str) -> dict[str, Any]:
    """Read a contract's source and a parsed summary of its surface.

    Args:
        path: Path to the contract file.

    Returns:
        A dict with `source` and a `summary` (functions, state vars, modifiers,
        events). Functions that could not be parsed are flagged `unauditable`.
    """
    return _get_engine().read_contract(path).model_dump()


def list_functions(path: str) -> dict[str, Any]:
    """List a contract's function signatures with visibility/mutability.

    Args:
        path: Path to the contract file.

    Returns:
        A dict with `functions` and any `unauditable_functions`.
    """
    return _get_engine().list_functions(path).model_dump()


def propose_patch(path: str, new_content: str) -> dict[str, Any]:
    """Stage a patch on a git branch WITHOUT touching the canonical file.

    Args:
        path: Path to the contract file (relative to the repo root).
        new_content: The full proposed source for the file.

    Returns:
        A dict with a `patch_id` and the staging `branch`.
    """
    return _get_engine().propose_patch(path, new_content).model_dump()


def diff_patch(patch_id: str) -> dict[str, Any]:
    """Return a unified diff for a staged patch, for human review.

    Args:
        patch_id: The id returned by `propose_patch`.

    Returns:
        A dict with the unified `diff`.
    """
    return _get_engine().diff_patch(patch_id).model_dump()


def apply_patch(patch_id: str) -> dict[str, Any]:
    """Apply a staged patch to the canonical file (Human-Checkpoint gated).

    Only call after the Human Checkpoint has approved (architecture.md §7).

    Args:
        patch_id: The id returned by `propose_patch`.

    Returns:
        A dict with `applied` and the new canonical `commit`.
    """
    return _get_engine().apply_patch(patch_id).model_dump()


def build_server() -> FastMCP:
    """Construct the FastMCP app with all five CodebaseMCP tools registered."""
    mcp = FastMCP("sentinel-codebase")
    for fn in (read_contract, list_functions, propose_patch, diff_patch, apply_patch):
        mcp.add_tool(fn, description=fn.__doc__)
    return mcp


def main() -> None:
    """Run the CodebaseMCP server over the env-selected transport (default stdio)."""
    run_server(build_server())


if __name__ == "__main__":
    main()
