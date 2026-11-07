// SPDX-License-Identifier: AGPL-3.0-only
// Primitive internals defined in-tree, named as the literature names them.
fn quarter_round(state: &mut [u32; 16]) { // expect: CR001
    state[0] = state[0].wrapping_add(state[4]);
}
pub fn chacha20_block(state: &mut [u32; 16]) { // expect: CR001
    quarter_round(state);
}
pub fn sha256_compress(state: &mut [u32; 8], block: &[u8; 64]) { // expect: CR001
    state[0] = state[0].wrapping_add(u32::from(block[0]));
}
fn fe_mul(a: u64, b: u64) -> u64 { // expect: CR001
    a.wrapping_mul(b)
}
pub fn poly1305_blocks(accumulator: &mut [u64; 3], block: &[u8; 16]) { // expect: CR001
    accumulator[0] = accumulator[0].wrapping_add(u64::from(block[0]));
}
