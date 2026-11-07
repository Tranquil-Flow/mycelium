// SPDX-License-Identifier: AGPL-3.0-only
//! SE-L10-CRYPTO-DEDUPE: `protocol::handoff_crypto` is a public API that `api` can call. The expected values are the
//! RFC vectors (independent of the implementation): RFC 7748 section 5.2 and 6.1, RFC 5869 A.1 and A.3 (the first
//! 32 output bytes are one HKDF-SHA256 expand block), RFC 8439 sections 2.8.2 and A.5 (ChaCha20-Poly1305).
use protocol::handoff_crypto::{
    aead_decrypt, aead_encrypt, hkdf_sha256_32, x25519_exchange, x25519_public,
};

fn h<const N: usize>(text: &str) -> [u8; N] {
    let clean: String = text.chars().filter(|c| c.is_ascii_hexdigit()).collect();
    let mut out = [0u8; N];
    assert_eq!(clean.len(), 2 * N);
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&clean[2 * i..2 * i + 2], 16).unwrap();
    }
    out
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[test]
fn x25519_matches_rfc7748() {
    // 5.2, first scalar multiplication.
    let scalar: [u8; 32] = h("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4");
    let point: [u8; 32] = h("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c");
    assert_eq!(
        hex(&x25519_exchange(&scalar, &point).unwrap()),
        "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552"
    );
    // 6.1 Diffie-Hellman: both parties derive the same secret.
    let alice: [u8; 32] = h("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a");
    let bob: [u8; 32] = h("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb");
    assert_eq!(
        hex(&x25519_public(&alice)),
        "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    );
    assert_eq!(
        hex(&x25519_public(&bob)),
        "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f"
    );
    let shared = "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742";
    assert_eq!(
        hex(&x25519_exchange(&alice, &x25519_public(&bob)).unwrap()),
        shared
    );
    assert_eq!(
        hex(&x25519_exchange(&bob, &x25519_public(&alice)).unwrap()),
        shared
    );
    // OpenSSL (so `cryptography`, so v0.1.4) refuses a low-order point: the all-zero result.
    assert_eq!(x25519_exchange(&alice, &[0u8; 32]), None);
}

#[test]
fn hkdf_sha256_matches_rfc5869() {
    // A.1, L = 42; the first 32 bytes.
    let okm = hkdf_sha256_32(
        &h::<22>("0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b"),
        &h::<13>("000102030405060708090a0b0c"),
        &h::<10>("f0f1f2f3f4f5f6f7f8f9"),
    );
    assert_eq!(
        hex(&okm),
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
    );
    // A.3, zero-length salt and info: an empty salt is HashLen zero bytes.
    let okm = hkdf_sha256_32(
        &h::<22>("0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b"),
        &[],
        &[],
    );
    assert_eq!(
        hex(&okm),
        "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d"
    );
}

#[test]
fn aead_matches_rfc8439_and_refuses_tampering() {
    // 2.8.2
    let key: [u8; 32] = h("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f");
    let nonce: [u8; 12] = h("070000004041424344454647");
    let aad = h::<12>("50515253c0c1c2c3c4c5c6c7");
    let plain = b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen would be it.";
    let sealed = aead_encrypt(&key, &nonce, plain, &aad);
    assert_eq!(sealed.len(), plain.len() + 16);
    assert_eq!(hex(&sealed[..16]), "d31a8d34648e60db7b86afbc53ef7ec2");
    assert_eq!(
        hex(&sealed[sealed.len() - 16..]),
        "1ae10b594f09e26a7e902ecbd0600691"
    );
    assert_eq!(
        aead_decrypt(&key, &nonce, &sealed, &aad).as_deref(),
        Some(&plain[..])
    );
    let mut bad = sealed.clone();
    bad[3] ^= 1;
    assert_eq!(aead_decrypt(&key, &nonce, &bad, &aad), None);
    assert_eq!(
        aead_decrypt(&key, &nonce, &sealed, &h::<12>("50515253c0c1c2c3c4c5c6c8")),
        None
    );
    assert_eq!(aead_decrypt(&key, &nonce, &sealed[..15], &aad), None);
    assert_eq!(aead_decrypt(&key, &nonce, &[], &aad), None);
    // A.5 (decryption test vector) opens to its plaintext.
    let key: [u8; 32] = h("1c9240a5eb55d38af333888604f6b5f0473917c1402b80099dca5cbc207075c0");
    let nonce: [u8; 12] = h("000000000102030405060708");
    let aad = h::<12>("f33388860000000000004e91");
    let sealed = h::<{ 265 + 16 }>(
        "64a0861575861af460f062c79be643bd5e805cfd345cf389f108670ac76c8cb2\
         4c6cfc18755d43eea09ee94e382d26b0bdb7b73c321b0100d4f03b7f355894cf\
         332f830e710b97ce98c8a84abd0b948114ad176e008d33bd60f982b1ff37c855\
         9797a06ef4f0ef61c186324e2b3506383606907b6a7c02b0f9f6157b53c867e4\
         b9166c767b804d46a59b5216cde7a4e99040c5a40433225ee282a1b0a06c523e\
         af4534d7f83fa1155b0047718cbc546a0d072b04b3564eea1b422273f548271a\
         0bb2316053fa76991955ebd63159434ecebb4e466dae5a1073a6727627097a10\
         49e617d91d361094fa68f0ff77987130305beaba2eda04df997b714d6c6f2c29\
         a6ad5cb4022b02709beead9d67890cbb22392336fea1851f38",
    );
    let opened = aead_decrypt(&key, &nonce, &sealed, &aad).expect("RFC 8439 A.5 opens");
    assert!(
        opened
            .starts_with(b"Internet-Drafts are draft documents valid for a maximum of six months")
    );
    assert_eq!(opened.len(), 265);
}
