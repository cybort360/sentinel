// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/// @title SubscriptionBilling
/// @notice DEMO ONLY — intentionally vulnerable. This is the primary target for
///         the SENTINEL War Room demo (architecture.md §12): a subscription
///         service with an automated on-chain settlement loop.
///
///         It contains two planted flaws for the Adversary Agent to find via
///         SimulationMCP:
///
///         1. CONGESTION / FEE-SPIKE DoS (100% revert rate). `processBilling()`
///            settles every subscriber in a single atomic loop. If ANY one
///            subscriber cannot cover its fee for that cycle — the common case
///            during a fee spike, when prepaid balances run thin — the whole
///            call reverts and NOBODY is billed. Under the fee-spike scenario
///            this yields a 100% revert rate for settlement.
///
///         2. REENTRANCY. `cancelSubscription()` refunds the prepaid balance via
///            an external call BEFORE zeroing state (checks-effects-interactions
///            violated), so a malicious subscriber contract can re-enter and
///            drain the contract.
///
///         NEVER deploy to a real network. Local Anvil fork only (CLAUDE.md
///         Golden Rule #3).
contract SubscriptionBilling {
    struct Subscriber {
        uint256 balance; // remaining prepaid balance
        uint256 feePerCycle; // amount billed each settlement cycle
        bool active;
    }

    address public immutable merchant;
    address[] public subscribers;
    mapping(address => Subscriber) public subs;

    event Subscribed(address indexed subscriber, uint256 feePerCycle, uint256 prepaid);
    event Deposited(address indexed subscriber, uint256 amount);
    event Settled(address indexed subscriber, uint256 amount);
    event Cancelled(address indexed subscriber, uint256 refunded);

    constructor(address _merchant) {
        require(_merchant != address(0), "merchant is zero");
        merchant = _merchant;
    }

    /// @notice Open a subscription, prepaying the attached ETH as balance.
    /// @param feePerCycle Amount charged to this subscriber each settlement.
    function subscribe(uint256 feePerCycle) external payable {
        require(!subs[msg.sender].active, "already subscribed");
        require(feePerCycle > 0, "fee is zero");
        subs[msg.sender] = Subscriber({balance: msg.value, feePerCycle: feePerCycle, active: true});
        subscribers.push(msg.sender);
        emit Subscribed(msg.sender, feePerCycle, msg.value);
    }

    /// @notice Top up an existing subscription's prepaid balance.
    function deposit() external payable {
        require(subs[msg.sender].active, "not subscribed");
        subs[msg.sender].balance += msg.value;
        emit Deposited(msg.sender, msg.value);
    }

    /// @notice Run one settlement cycle across every subscriber.
    /// @dev VULN #1 (congestion DoS): the loop is unbounded AND atomic. A single
    ///      underfunded subscriber trips the `require` and reverts the entire
    ///      batch, so during a fee spike no settlement ever succeeds — a 100%
    ///      revert rate. The fix surfaced in the demo is to make each charge
    ///      independent (skip/queue failures) rather than all-or-nothing.
    function processBilling() external {
        uint256 n = subscribers.length;
        for (uint256 i = 0; i < n; i++) {
            address account = subscribers[i];
            Subscriber storage s = subs[account];
            if (!s.active) {
                continue;
            }
            // One failure here aborts the whole batch (the planted DoS).
            require(s.balance >= s.feePerCycle, "insufficient balance");
            s.balance -= s.feePerCycle;
            (bool ok,) = merchant.call{value: s.feePerCycle}("");
            require(ok, "merchant transfer failed");
            emit Settled(account, s.feePerCycle);
        }
    }

    /// @notice Cancel a subscription and refund the remaining prepaid balance.
    /// @dev VULN #2 (reentrancy): the refund is sent BEFORE state is cleared, so
    ///      a contract subscriber can re-enter `cancelSubscription()` from its
    ///      receive hook and withdraw repeatedly. The fix is to zero state first
    ///      (checks-effects-interactions) or add a reentrancy guard.
    function cancelSubscription() external {
        Subscriber storage s = subs[msg.sender];
        require(s.active, "not subscribed");
        uint256 refund = s.balance;
        (bool ok,) = msg.sender.call{value: refund}("");
        require(ok, "refund failed");
        s.balance = 0;
        s.active = false;
        emit Cancelled(msg.sender, refund);
    }

    /// @notice Number of subscribers ever registered (active or not).
    function subscriberCount() external view returns (uint256) {
        return subscribers.length;
    }

    receive() external payable {}
}
