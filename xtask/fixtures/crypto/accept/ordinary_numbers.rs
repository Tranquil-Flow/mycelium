// SPDX-License-Identifier: AGPL-3.0-only
// Lone 0x36 / 0x5c (ASCII '6' and '\'), near-miss constants and test-vector digests are fine.
pub fn six(byte: u8) -> bool {
    byte == 0x36
}
pub fn backslash(byte: u8) -> bool {
    byte == 0x5c
}
pub const NEAR_MISS: [u32; 3] = [0x6a09, 0x0fffffff, 0x428a2f97];
pub const PORT_LIKE: u32 = 121_000;
// SHA-256("abc") from FIPS 180-4 appendix B.1: expected output, not an algorithm constant.
pub const ABC_DIGEST_HEAD: [u8; 4] = [0xba, 0x78, 0x16, 0xbf];
pub const SBOX_PREFIX_ONLY: [u8; 3] = [0x63, 0x7c, 0x77];
