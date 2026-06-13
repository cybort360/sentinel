"""A dependency-light Solidity *surface* parser (architecture.md §5.1).

This is intentionally not a full solc AST: it extracts the contract surface
(functions with visibility/mutability/modifiers/returns, state variables,
events, modifier definitions) using brace-balanced scanning. Its defining
property is robustness, per CLAUDE.md Rule 4: a function whose body is
truncated or malformed is flagged ``unauditable`` with a reason, and parsing
continues — one bad function never crashes the run, and the others are still
returned.

Known limitation: functions across multiple contracts in one file are returned
as a single flat list (the sandbox uses one contract per file).
"""

from __future__ import annotations

import re

from sentinel.mcp_servers.codebase_mcp.results import (
    ContractSummary,
    FunctionInfo,
    StateVariable,
)

_VISIBILITY = {"public", "external", "internal", "private"}
_MUTABILITY = {"view", "pure", "payable"}
_TAIL_SKIP = {"virtual", "override", "returns"}
_BRACKETS = {"(": ")", "{": "}", "[": "]"}

# Callable headers: function / constructor / receive / fallback / modifier.
_CALLABLE_RE = re.compile(
    r"\b(?P<kw>function|constructor|receive|fallback|modifier)\b\s*(?P<name>\w+)?\s*\("
)
_STRUCT_ENUM_RE = re.compile(r"\b(?:struct|enum)\s+\w+\s*\{")
_CONTRACT_RE = re.compile(r"\b(?:contract|library|interface)\s+(\w+)")
_PRAGMA_RE = re.compile(r"pragma\s+solidity\s+([^;]+);")
_EVENT_RE = re.compile(r"^\s*event\s+(\w+)")
_TOKEN_RE = re.compile(r"[A-Za-z_]\w*(?:\([^)]*\))?")


def strip_comments(source: str) -> str:
    """Replace comments with spaces, preserving length and string literals.

    Args:
        source: Raw Solidity source.

    Returns:
        Source with ``//`` and ``/* */`` comments blanked (newlines kept), so
        character offsets and line numbers still line up with the original.
    """
    out: list[str] = []
    i, n = 0, len(source)
    in_str: str | None = None
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if in_str is not None:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(nxt)
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
        elif c in ('"', "'"):
            in_str = c
            out.append(c)
            i += 1
        elif c == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
        elif c == "/" and nxt == "*":
            while i < n and not (
                source[i] == "*" and i + 1 < n and source[i + 1] == "/"
            ):
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _match_bracket(s: str, open_idx: int) -> tuple[str | None, int]:
    """Return ``(inner, end)`` for the bracket at ``open_idx``, skipping strings.

    Returns ``(None, len(s))`` if the bracket never balances (truncated input).
    """
    open_ch = s[open_idx]
    close_ch = _BRACKETS[open_ch]
    depth = 0
    i, n = open_idx, len(s)
    in_str: str | None = None
    while i < n:
        c = s[i]
        if in_str is not None:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        elif c in ('"', "'"):
            in_str = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return s[open_idx + 1 : i], i + 1
        i += 1
    return None, n


def _find_terminator(s: str, start: int) -> tuple[int, str]:
    """Find the next ``{`` or ``;`` from ``start`` (skipping strings)."""
    i, n = start, len(s)
    in_str: str | None = None
    while i < n:
        c = s[i]
        if in_str is not None:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        elif c in ('"', "'"):
            in_str = c
        elif c in "{;":
            return i, c
        i += 1
    return n, ""


def _parse_tail(tail: str) -> tuple[str | None, str | None, str | None, list[str]]:
    """Parse a function header tail into (visibility, mutability, returns, mods)."""
    returns: str | None = None
    ridx = tail.find("returns")
    head = tail[:ridx] if ridx >= 0 else tail
    if ridx >= 0:
        paren = tail.find("(", ridx)
        if paren >= 0:
            inner, _ = _match_bracket(tail, paren)
            returns = inner.strip() if inner is not None else None
    visibility: str | None = None
    mutability: str | None = None
    mods: list[str] = []
    for token in _TOKEN_RE.findall(head):
        base = token.split("(")[0]
        if base in _VISIBILITY:
            visibility = base
        elif base in _MUTABILITY:
            mutability = base
        elif base in _TAIL_SKIP:
            continue
        else:
            mods.append(token)
    return visibility, mutability, returns, mods


def _line_of(source: str, idx: int) -> int:
    """1-based line number of character offset ``idx``."""
    return source.count("\n", 0, idx) + 1


def _parse_one_callable(
    cleaned: str, m: re.Match[str]
) -> tuple[FunctionInfo | None, str | None, int]:
    """Parse a single callable starting at match ``m``.

    Returns ``(info, modifier_name, span_end)``. Exactly one of ``info`` /
    ``modifier_name`` is set: modifiers are tracked separately from functions.
    ``span_end`` is the offset just past this callable (for residue blanking).
    """
    kw = m.group("kw")
    name = m.group("name") or kw  # constructor/receive/fallback have no name
    paren_open = m.end() - 1
    params, params_end = _match_bracket(cleaned, paren_open)
    line = _line_of(cleaned, m.start())

    if params is None:
        info = FunctionInfo(
            name=name,
            kind=kw if kw != "function" else "function",
            signature=f"{name}(?)",
            line=line,
            unauditable=True,
            reason="could not parse parameter list (malformed/partial)",
        )
        return (None, name, m.end()) if kw == "modifier" else (info, None, m.end())

    term_idx, term_ch = _find_terminator(cleaned, params_end)
    tail = cleaned[params_end:term_idx]
    visibility, mutability, returns, mods = _parse_tail(tail)
    signature = f"{name}({params.strip()})"

    unauditable = False
    reason: str | None = None
    span_end = term_idx + 1
    if term_ch == "{":
        body, body_end = _match_bracket(cleaned, term_idx)
        if body is None:
            unauditable = True
            reason = (
                "unbalanced braces — truncated or malformed body (partial contract)"
            )
            span_end = len(cleaned)
        else:
            span_end = body_end
    elif term_ch == "":
        unauditable = True
        reason = "no body terminator found (truncated declaration)"
        span_end = len(cleaned)

    if kw == "modifier":
        return None, name, span_end

    info = FunctionInfo(
        name=name,
        kind="function" if kw == "function" else kw,
        signature=signature,
        visibility=visibility,
        mutability=mutability,
        modifiers=mods,
        returns=returns,
        line=line,
        unauditable=unauditable,
        reason=reason,
    )
    return info, None, span_end


def _parse_state_var(chunk: str) -> StateVariable | None:
    """Best-effort parse of one ``;``-terminated contract-scope declaration."""
    # Drop any leading contract/struct header braces left in the residue.
    chunk = chunk.rsplit("{", 1)[-1].replace("}", "").strip()
    if not chunk or chunk.startswith(("using", "import", "pragma", "event")):
        return None
    visibility = next((v for v in _VISIBILITY if re.search(rf"\b{v}\b", chunk)), None)
    if chunk.startswith("mapping"):
        inner, end = _match_bracket(chunk, chunk.find("("))
        if inner is None:
            return None
        rest = chunk[end:].split("=")[0].strip().split()
        if not rest:
            return None
        return StateVariable(
            name=rest[-1], type=f"mapping({inner.strip()})", visibility=visibility
        )
    head = chunk.split("=")[0].strip()
    tokens = head.split()
    if len(tokens) < 2:
        return None
    return StateVariable(name=tokens[-1], type=tokens[0], visibility=visibility)


def parse_source(path: str, source: str) -> ContractSummary:
    """Parse a Solidity source file into a :class:`ContractSummary`.

    Never raises on malformed input: parse problems become ``unauditable``
    function flags and ``parse_errors`` entries (CLAUDE.md Rule 4).

    Args:
        path: The source path (for the summary).
        source: Raw Solidity source.

    Returns:
        The parsed surface summary.
    """
    summary = ContractSummary(path=path)
    cleaned = strip_comments(source)

    pragma = _PRAGMA_RE.search(cleaned)
    summary.pragma = pragma.group(1).strip() if pragma else None
    summary.contracts = _CONTRACT_RE.findall(cleaned)

    blank = list(cleaned)
    for m in _CALLABLE_RE.finditer(cleaned):
        try:
            info, modifier_name, span_end = _parse_one_callable(cleaned, m)
        except Exception as exc:  # noqa: BLE001 — isolate one bad member (Rule 4)
            summary.parse_errors.append(
                f"member at line {_line_of(cleaned, m.start())}: {exc}"
            )
            continue
        if modifier_name is not None:
            summary.modifiers.append(modifier_name)
        if info is not None:
            summary.functions.append(info)
            if info.unauditable:
                summary.unauditable_functions.append(info.name)
        for i in range(m.start(), min(span_end, len(blank))):
            blank[i] = " "

    for m in _STRUCT_ENUM_RE.finditer(cleaned):
        _, end = _match_bracket(cleaned, cleaned.index("{", m.start()))
        for i in range(m.start(), min(end, len(blank))):
            blank[i] = " "

    residue = "".join(blank)
    for em in _EVENT_RE.finditer(residue):
        summary.events.append(em.group(1))
    for raw in residue.split(";"):
        if _EVENT_RE.match(raw):
            continue
        try:
            var = _parse_state_var(raw)
        except Exception as exc:  # noqa: BLE001 — never crash on a weird decl
            summary.parse_errors.append(f"state-var decl: {exc}")
            continue
        if var is not None:
            summary.state_variables.append(var)

    if not summary.contracts and not summary.functions:
        summary.parse_errors.append("no contract or function declarations found")
    return summary
