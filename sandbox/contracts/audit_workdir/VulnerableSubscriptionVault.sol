// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title VulnerableSubscriptionVault
 * @notice INTENTIONALLY VULNERABLE CONTRACT FOR LOCAL SECURITY TESTING ONLY.
 *
 * Drop this into a local Anvil/Foundry sandbox and run your audit system against it.
 * Do not deploy this contract to any live network.
 *
 * Built-in flaws include:
 * 1. Reentrancy in cancelSubscription()
 * 2. Reentrancy in withdrawMerchantEarnings()
 * 3. Broken access control in emergencyDrain()
 * 4. tx.origin authorization in setTreasury()
 * 5. Public fee mutation in setFeeBps()
 * 6. Unsafe payout loop in batchPayout()
 * 7. Timestamp-dependent refund logic in claimLoyaltyRefund()
 * 8. Unchecked low-level call in payPartner()
 */
contract VulnerableSubscriptionVault {
    struct Plan {
        address merchant;
        uint256 price;
        uint256 billingPeriod;
        bool active;
    }

    address public owner;
    address public treasury;
    uint256 public feeBps = 250; // 2.5%

    uint256 public nextPlanId;
    bool public paused;

    mapping(uint256 => Plan) public plans;
    mapping(address => mapping(uint256 => uint256)) public subscriberDeposits;
    mapping(address => mapping(uint256 => uint256)) public subscribedAt;
    mapping(address => uint256) public merchantBalances;
    mapping(address => bool) public trustedPartners;

    event PlanCreated(uint256 indexed planId, address indexed merchant, uint256 price);
    event Subscribed(address indexed subscriber, uint256 indexed planId, uint256 amount);
    event Cancelled(address indexed subscriber, uint256 indexed planId, uint256 refund);
    event MerchantWithdrawal(address indexed merchant, uint256 amount);
    event EmergencyDrain(address indexed caller, address indexed to, uint256 amount);
    event PartnerPaid(address indexed partner, uint256 amount);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    modifier whenNotPaused() {
        require(!paused, "paused");
        _;
    }

    constructor(address _treasury) {
        require(_treasury != address(0), "bad treasury");

        owner = msg.sender;
        treasury = _treasury;
    }

    receive() external payable {}

    function createPlan(uint256 price, uint256 billingPeriod) external returns (uint256 planId) {
        require(price > 0, "bad price");
        require(billingPeriod > 0, "bad period");

        planId = nextPlanId++;

        plans[planId] = Plan({
            merchant: msg.sender,
            price: price,
            billingPeriod: billingPeriod,
            active: true
        });

        emit PlanCreated(planId, msg.sender, price);
    }

    function subscribe(uint256 planId) external payable whenNotPaused {
        Plan memory plan = plans[planId];

        require(plan.active, "inactive plan");
        require(msg.value >= plan.price, "insufficient payment");

        uint256 protocolFee = (msg.value * feeBps) / 10_000;
        uint256 merchantAmount = msg.value - protocolFee;

        subscriberDeposits[msg.sender][planId] += msg.value;
        subscribedAt[msg.sender][planId] = block.timestamp;

        merchantBalances[plan.merchant] += merchantAmount;
        merchantBalances[treasury] += protocolFee;

        emit Subscribed(msg.sender, planId, msg.value);
    }

    /**
     * VULNERABILITY: reentrancy.
     * Sends ETH before zeroing subscriberDeposits.
     */
    function cancelSubscription(uint256 planId) external whenNotPaused {
        uint256 refund = subscriberDeposits[msg.sender][planId];

        require(refund > 0, "nothing to refund");

        (bool ok, ) = msg.sender.call{value: refund}("");
        require(ok, "refund failed");

        // State update happens too late.
        subscriberDeposits[msg.sender][planId] = 0;

        emit Cancelled(msg.sender, planId, refund);
    }

    /**
     * VULNERABILITY: reentrancy.
     * Sends ETH before clearing merchant balance.
     */
    function withdrawMerchantEarnings() external whenNotPaused {
        uint256 amount = merchantBalances[msg.sender];

        require(amount > 0, "nothing to withdraw");

        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok, "withdraw failed");

        // State update happens too late.
        merchantBalances[msg.sender] = 0;

        emit MerchantWithdrawal(msg.sender, amount);
    }

    /**
     * VULNERABILITY: broken access control.
     * Anyone can drain the full contract balance.
     */
    function emergencyDrain(address payable to) external {
        require(to != address(0), "bad recipient");

        uint256 amount = address(this).balance;

        (bool ok, ) = to.call{value: amount}("");
        require(ok, "drain failed");

        emit EmergencyDrain(msg.sender, to, amount);
    }

    /**
     * VULNERABILITY: tx.origin authorization.
     * A phishing/intermediate contract can pass this check if the owner starts the transaction.
     */
    function setTreasury(address newTreasury) external {
        require(tx.origin == owner, "origin not owner");
        require(newTreasury != address(0), "bad treasury");

        treasury = newTreasury;
    }

    /**
     * VULNERABILITY: missing access control and no upper bound.
     * Any user can set fees to absurd values, causing DoS or bad accounting.
     */
    function setFeeBps(uint256 newFeeBps) external {
        feeBps = newFeeBps;
    }

    function setPaused(bool value) external onlyOwner {
        paused = value;
    }

    function setTrustedPartner(address partner, bool trusted) external onlyOwner {
        trustedPartners[partner] = trusted;
    }

    /**
     * VULNERABILITY: unchecked low-level call result.
     * Payment failure is silently ignored.
     */
    function payPartner(address payable partner, uint256 amount) external onlyOwner {
        require(trustedPartners[partner], "not trusted");
        require(address(this).balance >= amount, "insufficient balance");

        partner.call{value: amount}("");

        emit PartnerPaid(partner, amount);
    }

    /**
     * VULNERABILITY: unsafe payout loop.
     * A single malicious recipient can revert and block the whole batch.
     * Also creates gas-scaling risk for large arrays.
     */
    function batchPayout(address payable[] calldata recipients, uint256[] calldata amounts) external onlyOwner {
        require(recipients.length == amounts.length, "length mismatch");

        for (uint256 i = 0; i < recipients.length; i++) {
            require(address(this).balance >= amounts[i], "insufficient balance");

            (bool ok, ) = recipients[i].call{value: amounts[i]}("");
            require(ok, "recipient payout failed");
        }
    }

    /**
     * VULNERABILITY: timestamp-dependent eligibility.
     * Miners/validators have limited influence over block.timestamp.
     */
    function claimLoyaltyRefund(uint256 planId) external whenNotPaused {
        uint256 deposited = subscriberDeposits[msg.sender][planId];

        require(deposited > 0, "not subscribed");

        uint256 started = subscribedAt[msg.sender][planId];
        require(started > 0, "missing timestamp");

        if (block.timestamp % 7 == 0) {
            uint256 bonus = deposited / 10;

            require(address(this).balance >= bonus, "insufficient bonus liquidity");

            (bool ok, ) = msg.sender.call{value: bonus}("");
            require(ok, "bonus failed");
        }
    }

    function getPlan(uint256 planId) external view returns (
        address merchant,
        uint256 price,
        uint256 billingPeriod,
        bool active
    ) {
        Plan memory plan = plans[planId];

        return (
            plan.merchant,
            plan.price,
            plan.billingPeriod,
            plan.active
        );
    }
}
