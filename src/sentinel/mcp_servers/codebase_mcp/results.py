"""Typed result models for CodebaseMCP (architecture.md §5.1).

The parser produces a :class:`ContractSummary` — a lightweight "AST summary" of
functions, state variables, modifiers, and events. Per CLAUDE.md Rule 4, a
function whose body cannot be parsed (truncated / malformed / partial contract)
is marked ``unauditable`` with a reason rather than aborting the whole parse.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FunctionInfo(BaseModel):
    """A single parsed function (or constructor/receive/fallback)."""

    name: str
    kind: str = "function"  # function | constructor | receive | fallback
    signature: str
    visibility: str | None = None
    mutability: str | None = None
    modifiers: list[str] = Field(default_factory=list)
    returns: str | None = None
    line: int | None = None
    unauditable: bool = False
    reason: str | None = None


class StateVariable(BaseModel):
    """A best-effort parsed contract-level state variable."""

    name: str
    type: str
    visibility: str | None = None


class ContractSummary(BaseModel):
    """The parsed surface of a Solidity source file (architecture.md §5.1)."""

    path: str
    pragma: str | None = None
    contracts: list[str] = Field(default_factory=list)
    functions: list[FunctionInfo] = Field(default_factory=list)
    state_variables: list[StateVariable] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    modifiers: list[str] = Field(default_factory=list)
    unauditable_functions: list[str] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)


class ReadResult(BaseModel):
    """Result of ``read_contract``: source plus its parsed summary."""

    path: str
    source: str
    summary: ContractSummary


class ListResult(BaseModel):
    """Result of ``list_functions``: signatures with visibility/mutability."""

    path: str
    functions: list[FunctionInfo] = Field(default_factory=list)
    unauditable_functions: list[str] = Field(default_factory=list)


class ProposeResult(BaseModel):
    """Result of ``propose_patch``: a staged-but-unapplied patch."""

    patch_id: str
    path: str
    branch: str
    base_commit: str


class DiffResult(BaseModel):
    """Result of ``diff_patch``: a unified diff for human review."""

    patch_id: str
    path: str
    diff: str


class ApplyResult(BaseModel):
    """Result of ``apply_patch``: the patch applied to the canonical file."""

    patch_id: str
    path: str
    applied: bool
    commit: str | None = None
