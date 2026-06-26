
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract UnsupportedArrayConstructorUpload {
    address[] public owners;

    constructor(address[] memory _owners) {
        owners = _owners;
    }
}
