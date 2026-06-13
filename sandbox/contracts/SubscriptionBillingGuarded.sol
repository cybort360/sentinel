// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/// @title SubscriptionBillingGuarded
/// @notice DEMO ONLY — the "patch v1" variant of SubscriptionBilling from the
///         War Room demo (architecture.md §12, round 1): a reentrancy guard has
///         been added to `cancelSubscription()`. It is otherwise byte-for-byte
///         the same logic as SubscriptionBilling, so a `measure_gas` comparison
///         of `cancelSubscription()` between the two isolates exactly the cost
///         the guard adds — the +gas the Adversary Agent vetoes on.
///
///         The congestion DoS in `processBilling()` is deliberately NOT fixed
///         here: that tension is what later rounds of the demo negotiate over.
///
///         NEVER deploy to a real network. Local Anvil fork only (CLAUDE.md
///         Golden Rule #3).
contract SubscriptionBillingGuarded {
    struct Subscriber {
        uint256 balance;
        uint256 feePerCycle;
        bool active;
    }

    // OpenZeppelin-style 1/2 reentrancy lock (avoids the 20k zero->nonzero SSTORE).
    uint256 private constant _NOT_ENTERED = 1;
    uint256 private constant _ENTERED = 2;
    uint256 private _status = _NOT_ENTERED;

    address public immutable merchant;
    address[] public subscribers;
    mapping(address => Subscriber) public subs;

    event Subscribed(address indexed subscriber, uint256 feePerCycle, uint256 prepaid);
    event Deposited(address indexed subscriber, uint256 amount);
    event Settled(address indexed subscriber, uint256 amount);
    event Cancelled(address indexed subscriber, uint256 refunded);

    /// @dev Reentrancy guard: rejects nested entry into a guarded function.
    modifier nonReentrant() {
        require(_status != _ENTERED, "reentrant call");
        _status = _ENTERED;
        _;
        _status = _NOT_ENTERED;
    }

    constructor(address _merchant) {
        require(_merchant != address(0), "merchant is zero");
        merchant = _merchant;
    }

    function subscribe(uint256 feePerCycle) external payable {
        require(!subs[msg.sender].active, "already subscribed");
        require(feePerCycle > 0, "fee is zero");
        subs[msg.sender] = Subscriber({balance: msg.value, feePerCycle: feePerCycle, active: true});
        subscribers.push(msg.sender);
        emit Subscribed(msg.sender, feePerCycle, msg.value);
    }

    function deposit() external payable {
        require(subs[msg.sender].active, "not subscribed");
        subs[msg.sender].balance += msg.value;
        emit Deposited(msg.sender, msg.value);
    }

    /// @dev Same atomic settlement loop as the naive contract — still vulnerable
    ///      to the fee-spike DoS by design (see contract-level note).
    function processBilling() external {
        uint256 n = subscribers.length;
        for (uint256 i = 0; i < n; i++) {
            address account = subscribers[i];
            Subscriber storage s = subs[account];
            if (!s.active) {
                continue;
            }
            require(s.balance >= s.feePerCycle, "insufficient balance");
            s.balance -= s.feePerCycle;
            (bool ok,) = merchant.call{value: s.feePerCycle}("");
            require(ok, "merchant transfer failed");
            emit Settled(account, s.feePerCycle);
        }
    }

    /// @notice Cancel and refund — now reentrancy-guarded (the planted patch).
    function cancelSubscription() external nonReentrant {
        Subscriber storage s = subs[msg.sender];
        require(s.active, "not subscribed");
        uint256 refund = s.balance;
        (bool ok,) = msg.sender.call{value: refund}("");
        require(ok, "refund failed");
        s.balance = 0;
        s.active = false;
        emit Cancelled(msg.sender, refund);
    }

    function subscriberCount() external view returns (uint256) {
        return subscribers.length;
    }

    receive() external payable {}
}
