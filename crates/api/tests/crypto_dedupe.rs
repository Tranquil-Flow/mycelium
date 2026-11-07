// SPDX-License-Identifier: AGPL-3.0-only
//! SE-CRYPTO-DEDUPE: the pairing handoff has ONE crypto implementation, `protocol::handoff_crypto`
//! (RU-010). `api::handoff::crypto` may only delegate to it: no second call site of the audited
//! crates inside `api`, and the delegating functions return what the protocol functions return.
use api::handoff::crypto;
use protocol::handoff_crypto as shared;

// The crate manifest still lists the three packages: `lock_edge_check.py` refuses a lane lock change that
// removes an edge, so dropping them is the integrator's (seam SE-L10-API-DEPS); no source may use them.
const ADMITTED_CRATES: [&str; 3] = ["x25519_dalek", "hkdf", "chacha20poly1305"];

fn sources() -> Vec<(String, String)> {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let mut found = Vec::new();
    for dir in ["src", "examples", "tests"] {
        for entry in std::fs::read_dir(root.join(dir)).expect("dir") {
            let path = entry.expect("entry").path();
            let own = path.file_name().is_some_and(|n| n == "crypto_dedupe.rs");
            if path.extension().is_some_and(|e| e == "rs") && !own {
                let text = std::fs::read_to_string(&path).expect("source");
                found.push((path.display().to_string(), text));
            }
        }
    }
    found
}

#[test]
fn api_names_none_of_the_primitive_crates() {
    let files = sources();
    assert!(files.len() > 40, "the scan saw the crate sources");
    for (file, text) in &files {
        for name in ADMITTED_CRATES {
            let hit = text
                .lines()
                .filter(|line| !line.trim_start().starts_with("//"))
                .any(|line| {
                    line.contains(&format!("{name}::")) || line.contains(&format!("use {name}"))
                });
            assert!(
                !hit,
                "{file} still names `{name}`; use protocol::handoff_crypto"
            );
        }
    }
}

#[test]
fn the_api_names_return_the_protocol_answers() {
    let (secret, peer) = ([0x11u8; 32], [0x22u8; 32]);
    assert_eq!(
        crypto::x25519_public(&secret),
        shared::x25519_public(&secret)
    );
    let peer_public = shared::x25519_public(&peer);
    assert_eq!(
        crypto::x25519(&secret, &peer_public),
        shared::x25519_exchange(&secret, &peer_public)
    );
    assert_eq!(
        crypto::x25519(&secret, &[0u8; 32]),
        None,
        "the all-zero point is refused as in OpenSSL"
    );
    assert_eq!(
        crypto::derive_key_32(b"ikm", b"salt", b"info"),
        shared::hkdf_sha256_32(b"ikm", b"salt", b"info")
    );
    let (key, nonce, aad) = ([3u8; 32], [4u8; 12], b"aad".as_slice());
    let sealed = crypto::aead_seal(&key, &nonce, b"plain", aad);
    assert_eq!(sealed, shared::aead_encrypt(&key, &nonce, b"plain", aad));
    assert_eq!(
        crypto::aead_open(&key, &nonce, &sealed, aad),
        shared::aead_decrypt(&key, &nonce, &sealed, aad)
    );
}
