// SPDX-License-Identifier: AGPL-3.0-only
// 0x6a09e667 and 0x428a2f98 in a comment, "expand 32-byte k" in prose, are not code.
/// The SHA-256 initial value starts 0x6a09e667 (FIPS 180-4); this doc line is not code either.
pub fn describe() -> &'static str {
    "sha256 hmac hkdf x25519 chacha20 poly1305 are admitted-crate names; 0x6a09e667 is quoted"
}
pub fn verify_sha256_digest(expected_len: usize) -> usize {
    let hmac_key_len = 32;
    let sha256_len = 32;
    expected_len + hmac_key_len + sha256_len
}
