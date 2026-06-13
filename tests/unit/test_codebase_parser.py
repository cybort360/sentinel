"""Unit tests for the CodebaseMCP Solidity surface parser.

Covers the happy path against the real sandbox contract and — critically — the
Rule 4 behaviour: a partial/unparseable contract flags the broken function as
``unauditable`` per-function and still returns the rest, without crashing.
"""

from __future__ import annotations

from pathlib import Path

from sentinel.mcp_servers.codebase_mcp.parser import parse_source

_SANDBOX = Path(__file__).resolve().parents[2] / "sandbox" / "contracts"


def _fn(summary: object, name: str) -> object:
    funcs = summary.functions  # type: ignore[attr-defined]
    return next(f for f in funcs if f.name == name)


def test_parses_subscription_billing_surface() -> None:
    source = (_SANDBOX / "SubscriptionBilling.sol").read_text()
    summary = parse_source("SubscriptionBilling.sol", source)

    assert summary.contracts == ["SubscriptionBilling"]
    assert summary.pragma is not None and "0.8" in summary.pragma
    names = {f.name for f in summary.functions}
    assert {"subscribe", "deposit", "processBilling", "cancelSubscription"} <= names
    # Nothing in a well-formed contract should be unauditable.
    assert summary.unauditable_functions == []

    subscribe = _fn(summary, "subscribe")
    assert subscribe.visibility == "external"
    assert subscribe.mutability == "payable"

    count = _fn(summary, "subscriberCount")
    assert count.visibility == "external"
    assert count.mutability == "view"
    assert count.returns is not None and "uint256" in count.returns


def test_parses_guarded_modifier_usage() -> None:
    source = (_SANDBOX / "SubscriptionBillingGuarded.sol").read_text()
    summary = parse_source("SubscriptionBillingGuarded.sol", source)

    assert "nonReentrant" in summary.modifiers  # the modifier *definition*
    cancel = _fn(summary, "cancelSubscription")
    assert "nonReentrant" in cancel.modifiers  # the modifier *usage*
    assert cancel.unauditable is False


def test_partial_contract_flags_only_the_broken_function() -> None:
    # `broken` is truncated mid-body (missing closing brace) — a partial
    # contract. `ok` before it and `tail` after must still parse.
    source = """
    pragma solidity ^0.8.19;
    contract Partial {
        uint256 public value;

        function ok() external view returns (uint256) {
            return value;
        }

        function broken() external {
            value = 1;
            if (value > 0) {
                value = 2;

        function tail() external pure returns (bool) {
            return true;
        }
    }
    """
    summary = parse_source("Partial.sol", source)

    names = {f.name for f in summary.functions}
    assert {"ok", "broken", "tail"} <= names

    ok = _fn(summary, "ok")
    assert ok.unauditable is False

    broken = _fn(summary, "broken")
    assert broken.unauditable is True
    assert broken.reason is not None
    assert "broken" in summary.unauditable_functions


def test_garbage_input_does_not_crash() -> None:
    summary = parse_source("garbage.sol", "\x00 not solidity at all }{}{ ;;; ")
    assert summary.functions == []
    assert summary.parse_errors  # noted, not raised
