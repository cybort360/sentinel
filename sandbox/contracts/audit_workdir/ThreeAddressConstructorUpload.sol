
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract ThreeAddressConstructorUpload {
    address public a;
    address public b;
    address public c;

    constructor(address _a, address _b, address _c) {
        a = _a;
        b = _b;
        c = _c;
    }
}
