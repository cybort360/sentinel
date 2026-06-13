// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/// @title YieldVault
/// @notice DEMO ONLY — intentionally vulnerable. This is the SECOND contract,
///         used for the cross-session memory demo (architecture.md §6.4). Its
///         vulnerability class is deliberately DIFFERENT from
///         SubscriptionBilling's (reentrancy / congestion) so that retrieval
///         from MemoryMCP must generalize across vulnerability classes rather
///         than being wired to one demo path.
///
///         VULNERABILITY CLASS: BROKEN ACCESS CONTROL. The privileged,
///         fund-moving functions `setOperator()` and `emergencyWithdraw()` are
///         missing their owner/operator guard, so ANY caller can reassign the
///         operator to an address they control and then sweep the entire vault.
///
///         NEVER deploy to a real network. Local Anvil fork only (CLAUDE.md
///         Golden Rule #3).
contract YieldVault {
    address public owner;
    address public operator;
    mapping(address => uint256) public shares;
    uint256 public totalShares;

    event Deposited(address indexed user, uint256 amount);
    event Withdrawn(address indexed user, uint256 amount);
    event OperatorChanged(address indexed previous, address indexed current);
    event EmergencyDrained(address indexed to, uint256 amount);

    constructor() {
        owner = msg.sender;
        operator = msg.sender;
    }

    /// @notice Deposit ETH and receive 1:1 shares.
    function deposit() external payable {
        require(msg.value > 0, "zero deposit");
        shares[msg.sender] += msg.value;
        totalShares += msg.value;
        emit Deposited(msg.sender, msg.value);
    }

    /// @notice Redeem shares for the underlying ETH.
    function withdraw(uint256 amount) external {
        require(shares[msg.sender] >= amount, "insufficient shares");
        shares[msg.sender] -= amount;
        totalShares -= amount;
        (bool ok,) = msg.sender.call{value: amount}("");
        require(ok, "transfer failed");
        emit Withdrawn(msg.sender, amount);
    }

    /// @notice Reassign the privileged operator role.
    /// @dev VULN (access control): this should be `onlyOwner`. As written, any
    ///      account can claim the operator role — the first half of the drain.
    function setOperator(address newOperator) external {
        require(newOperator != address(0), "operator is zero");
        emit OperatorChanged(operator, newOperator);
        operator = newOperator;
    }

    /// @notice Emergency-drain the whole vault balance to the operator.
    /// @dev VULN (access control): this should be `onlyOwner`/`onlyOperator` AND
    ///      paired with a guarded `setOperator`. As written, any caller can set
    ///      themselves as operator (above) and then call this to steal every
    ///      depositor's funds.
    function emergencyWithdraw() external {
        uint256 bal = address(this).balance;
        (bool ok,) = operator.call{value: bal}("");
        require(ok, "drain failed");
        emit EmergencyDrained(operator, bal);
    }
}
