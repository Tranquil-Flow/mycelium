// SPDX-License-Identifier: AGPL-3.0-only
//! The three primitives of the v0.1.4 encrypted invitation handoff
//! (`mycelium_mobile/android_onboarding.py`, `mycelium_a13_macos/pairing.py`): X25519, HKDF-SHA256 and
//! ChaCha20-Poly1305 (RFC 7748 / RFC 5869 / RFC 8439), delegated to the audited `x25519-dalek`, `hkdf` and
//! `chacha20poly1305` crates (the admitted lock revision of RU-010). This module only fixes the oracle's calling
//! conventions: pure functions over caller-supplied bytes, no RNG, no clock, no I/O, no panics on any input.
//!
//! **Public API (SE-L10-CRYPTO-DEDUPE).** This is the one handoff-crypto implementation in the workspace:
//! `protocol::android_onboarding` uses it and `api` (the pairing handoff of `DemoAuthority.approve_pairing` /
//! `RemoteMacMember.accept_handoff`) is meant to call it instead of carrying its own copy of the same glue. The
//! contract below is the oracle's and must not drift (`tests/handoff_crypto_public.rs`, RFC vectors, plus the
//! executed android-onboarding corpus):
//! - [`x25519_exchange`] refuses an all-zero shared secret (a low-order point), as OpenSSL / `cryptography` do;
//! - [`hkdf_sha256_32`] always derives exactly 32 bytes (one expand block); an empty salt means HashLen zeros;
//! - [`aead_encrypt`] returns `ciphertext || 16-byte tag`; [`aead_decrypt`] returns `None` for any
//!   authentication failure, a truncated input included.
use chacha20poly1305::{
    ChaCha20Poly1305, KeyInit,
    aead::{Aead, Payload},
};
use hkdf::Hkdf;
use sha2::Sha256;

/// `X25519PrivateKey.public_key().public_bytes_raw()`: the public key of a 32-byte secret (RFC 7748 clamps it).
pub fn x25519_public(secret: &[u8; 32]) -> [u8; 32] {
    x25519_dalek::x25519(*secret, x25519_dalek::X25519_BASEPOINT_BYTES)
}

/// `private.exchange(public)`: the 32-byte shared secret, or `None` when it is all zero (OpenSSL, hence
/// `cryptography`, refuses that result).
pub fn x25519_exchange(secret: &[u8; 32], peer: &[u8; 32]) -> Option<[u8; 32]> {
    let shared = x25519_dalek::x25519(*secret, *peer);
    shared.iter().any(|b| *b != 0).then_some(shared)
}

/// `HKDF(SHA256, length=32, salt, info).derive(secret)` (an empty salt is HashLen zero bytes, RFC 5869): the
/// 32 output bytes.
pub fn hkdf_sha256_32(secret: &[u8], salt: &[u8], info: &[u8]) -> [u8; 32] {
    let salt = (!salt.is_empty()).then_some(salt);
    let mut okm = [0u8; 32];
    // 32 bytes is far below the 255 * HashLen limit, so expand cannot fail.
    Hkdf::<Sha256>::new(salt, secret)
        .expand(info, &mut okm)
        .unwrap_or_else(|_| unreachable!("32 bytes is within the HKDF-SHA256 output limit"));
    okm
}

/// `ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)`: `ciphertext || 16-byte tag` (the output is 16 bytes
/// longer than `plaintext`).
pub fn aead_encrypt(key: &[u8; 32], nonce: &[u8; 12], plaintext: &[u8], aad: &[u8]) -> Vec<u8> {
    ChaCha20Poly1305::new(key.into())
        .encrypt(
            nonce.into(),
            Payload {
                msg: plaintext,
                aad,
            },
        )
        .unwrap_or_else(|_| unreachable!("a sealed message this size cannot overflow the AEAD"))
}

/// `ChaCha20Poly1305(key).decrypt(nonce, sealed, aad)` over `ciphertext || tag`; `None` is `InvalidTag` (wrong key,
/// nonce, aad, a flipped bit or an input shorter than the tag).
pub fn aead_decrypt(
    key: &[u8; 32],
    nonce: &[u8; 12],
    sealed: &[u8],
    aad: &[u8],
) -> Option<Vec<u8>> {
    ChaCha20Poly1305::new(key.into())
        .decrypt(nonce.into(), Payload { msg: sealed, aad })
        .ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn h(s: &str) -> Vec<u8> {
        hex::decode(s.replace([' ', '\n'], "")).unwrap()
    }

    #[test]
    fn rfc7748_x25519_vectors() {
        let alice: [u8; 32] = h("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
            .try_into()
            .unwrap();
        let bob_pub: [u8; 32] =
            h("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
                .try_into()
                .unwrap();
        assert_eq!(
            hex::encode(x25519_public(&alice)),
            "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
        );
        assert_eq!(
            hex::encode(x25519_exchange(&alice, &bob_pub).unwrap()),
            "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
        );
        assert_eq!(x25519_exchange(&alice, &[0u8; 32]), None);
    }

    #[test]
    fn rfc5869_hkdf_case_one_first_block() {
        // RFC 5869 A.1 (L = 42); the first 32 bytes are one expand block.
        let okm = hkdf_sha256_32(
            &h("0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b"),
            &h("000102030405060708090a0b0c"),
            &h("f0f1f2f3f4f5f6f7f8f9"),
        );
        assert_eq!(
            hex::encode(okm),
            "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
        );
    }

    #[test]
    fn rfc8439_aead_vector() {
        let key: [u8; 32] = h("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f")
            .try_into()
            .unwrap();
        let nonce: [u8; 12] = h("070000004041424344454647").try_into().unwrap();
        let aad = h("50515253c0c1c2c3c4c5c6c7");
        let plain = b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen would be it.";
        let sealed = aead_encrypt(&key, &nonce, plain, &aad);
        assert_eq!(
            hex::encode(&sealed[..16]),
            "d31a8d34648e60db7b86afbc53ef7ec2"
        );
        assert_eq!(
            hex::encode(&sealed[sealed.len() - 16..]),
            "1ae10b594f09e26a7e902ecbd0600691"
        );
        assert_eq!(
            aead_decrypt(&key, &nonce, &sealed, &aad).as_deref(),
            Some(&plain[..])
        );
        let mut bad = sealed.clone();
        bad[3] ^= 1;
        assert_eq!(aead_decrypt(&key, &nonce, &bad, &aad), None);
        assert_eq!(aead_decrypt(&key, &nonce, &sealed[..15], &aad), None);
    }

    #[test]
    fn rfc8439_poly1305_vector() {
        // RFC 8439 section 2.5.2, through the audited crate (the removed hand-written core had this test).
        use poly1305::{Key, Poly1305, universal_hash::KeyInit};
        let key: [u8; 32] = h("85d6be7857556d337f4452fe42d506a80103808afb0db2fd4abff6af4149f51b")
            .try_into()
            .unwrap();
        let tag = Poly1305::new(Key::from_slice(&key))
            .compute_unpadded(b"Cryptographic Forum Research Group");
        assert_eq!(hex::encode(tag), "a8061dc1305136c6c22b8baf0c0127a9");
    }
}
