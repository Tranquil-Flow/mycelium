// SPDX-License-Identifier: AGPL-3.0-only
// RU-010: admitted crates, wrapped under primitive-looking names, are fine.
use chacha20poly1305::{ChaCha20Poly1305, KeyInit};
use hkdf::Hkdf;
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};

pub fn hmac_sha256(key: &[u8], message: &[u8]) -> Result<Vec<u8>, String> {
    let mut mac = Hmac::<Sha256>::new_from_slice(key).map_err(|e| e.to_string())?;
    mac.update(message);
    Ok(mac.finalize().into_bytes().to_vec())
}
pub fn hkdf_expand(prk: &[u8], info: &[u8], out: &mut [u8]) -> Result<(), String> {
    let kdf = Hkdf::<Sha256>::from_prk(prk).map_err(|e| e.to_string())?;
    kdf.expand(info, out).map_err(|e| e.to_string())
}
pub fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes).iter().map(|b| format!("{b:02x}")).collect()
}
pub fn cipher(key: &[u8; 32]) -> ChaCha20Poly1305 {
    ChaCha20Poly1305::new(key.into())
}
