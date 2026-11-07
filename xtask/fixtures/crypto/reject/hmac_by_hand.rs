// SPDX-License-Identifier: AGPL-3.0-only
// RFC 2104: HMAC written out with its own ipad/opad instead of the admitted `hmac` crate.
pub fn hmac_sha256(key: &[u8], message: &[u8]) -> Vec<u8> { // expect: CR001
    let mut inner: Vec<u8> = key.iter().map(|byte| byte ^ 0x36).collect();
    inner.extend_from_slice(message);
    let mut outer: Vec<u8> = key.iter().map(|byte| byte ^ 0x5c).collect();
    outer.extend_from_slice(&inner);
    outer
}
// HKDF (RFC 5869) expanded by hand, no admitted crate in the body.
pub fn hkdf_expand(prk: &[u8], info: &[u8], length: usize) -> Vec<u8> { // expect: CR001
    let mut block = prk.to_vec();
    block.extend_from_slice(info);
    block.truncate(length);
    block
}
