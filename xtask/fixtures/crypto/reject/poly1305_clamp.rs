// SPDX-License-Identifier: AGPL-3.0-only
// RFC 8439 section 2.5: the r clamp and the prime 2^130 - 5.
pub const CLAMP: u128 = 0x0ffffffc0ffffffc0ffffffc0fffffff; // expect: CR001
pub const CLAMP_LOW: u64 = 0x0ffffffc0fffffff; // expect: CR001
pub const P_TOP: u128 = 0x3fffffffffffffffb; // expect: CR001
