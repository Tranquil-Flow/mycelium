// SPDX-License-Identifier: AGPL-3.0-only
// SHA-1 (FIPS 180-4 section 5.3.1), Keccak-f round constants (FIPS 202 section 3.2.5), AES S-box row 0.
pub const SHA1_INIT: [u32; 5] = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0]; // expect: CR001
pub const RC: [u64; 3] = [0x8000000000008082, 0x800000000000808a, 0x8000000080008000]; // expect: CR001
pub const SBOX_ROW: [u8; 8] = [0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5]; // expect: CR001
