// SPDX-License-Identifier: AGPL-3.0-only
// RFC 7748 section 5: a24 = (486662 - 2) / 4 = 121665; 51-bit limb mask of the field 2^255 - 19.
pub const A24: u64 = 121665; // expect: CR001
pub const MONTGOMERY_A: u64 = 486_662; // expect: CR001
pub const LIMB_MASK: u64 = 0x7ffffffffffff; // expect: CR001
pub fn montgomery_ladder(scalar: &[u8; 32], point: &[u8; 32]) -> [u8; 32] { // expect: CR001
    let mut out = *point;
    out[0] ^= scalar[0];
    out
}
