// SPDX-License-Identifier: AGPL-3.0-only
// Hand-written SHA-256 tables: FIPS 180-4 section 5.3.3 (initial hash) and 4.2.2 (first K words).
pub const H0: [u32; 8] = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]; // expect: CR001
pub const K: [u32; 4] = [0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5]; // expect: CR001
pub const SHA512_H0: u64 = 0x6a09e667f3bcc908; // expect: CR001
