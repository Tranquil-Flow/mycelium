// SPDX-License-Identifier: AGPL-3.0-only
//! PH3a's carried follow-ups on the packaged owner authority (REPORT-laneS1R, REVIEW-laneS1R), each
//! against v0.1.4 @ f4e366e9, through the packaged executable's real composition and its page's
//! routes (`support/packaged_macs.rs`).
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use desktop::lifecycle::Poll;
use macs::{Mac, consent, private, state_root};
use node_service::packaged_network::NetworkConfig;
use serde_json::json;
use std::path::Path;

fn launch(state: &Path) -> Mac {
    Mac::launch(state, NetworkConfig::default())
}
fn refusal(answer: &(u16, serde_json::Value)) -> (u16, String) {
    (
        answer.0,
        answer.1["error"]
            .as_str()
            .unwrap_or("<answered>")
            .to_owned(),
    )
}
/// An owner whose swarm exists, quit cleanly.
fn created_owner(tag: &str) -> std::path::PathBuf {
    let state = state_root(tag);
    let owner = launch(&state);
    let created = owner.post("/api/create", json!({"consent": consent(4_000_000_000)}));
    assert_eq!(created.0, 200, "create: {}", created.1);
    assert_eq!(owner.quit(), Ok(Poll::Verified));
    state
}

/// A swarm whose seed key is gone does not come back with a fresh, unbound key: v0.1.4's
/// `_start_runtime` loads the key bound to the seed database (`load_bound_seed_signer`,
/// mycelium_seed/authority.py:113-139) and refuses `seed_authority_key_missing`; nothing is created.
#[test]
fn packaged_owner_resume_refuses_a_lost_seed_key() {
    let state = created_owner("lost-seed-key");
    let key = private(&state).join("seed-identity.json");
    std::fs::remove_file(&key).unwrap();
    let owner = launch(&state);
    for _attempt in 0..2 {
        assert_eq!(
            refusal(&owner.post("/api/start", json!({}))),
            (409, "seed_authority_key_missing".to_owned())
        );
        assert!(!key.exists(), "a fresh seed key was created");
    }
    assert_eq!(owner.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// A regular file where the seed's TLS directory was is an identity loss, never fresh material
/// (`_start_runtime`, authority.py:508-512): `authority_tls_identity_missing` (REVIEW-laneS1R M4).
#[test]
fn packaged_owner_resume_refuses_a_regular_file_at_the_tls_directory() {
    let state = created_owner("tls-file");
    let tls = private(&state).join("seed-rs/tls");
    std::fs::remove_dir_all(&tls).unwrap();
    std::fs::write(&tls, b"not a directory").unwrap();
    let owner = launch(&state);
    assert_eq!(
        refusal(&owner.post("/api/start", json!({}))),
        (409, "authority_tls_identity_missing".to_owned())
    );
    assert_eq!(std::fs::read(&tls).unwrap(), b"not a directory");
    assert_eq!(owner.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// A fresh owner (no swarm, nothing prepared) stops and starts again: `/api/stop` then `/api/start`
/// answer 200 (app.py:1626-1640; the partial-state guard does not fire, REVIEW-laneS1R M6).
#[test]
fn packaged_fresh_owner_stops_and_starts_again() {
    let state = state_root("fresh-stop-start");
    let owner = launch(&state);
    let stopped = owner.post("/api/stop", json!({}));
    assert_eq!(stopped.0, 200, "stop: {}", stopped.1);
    let started = owner.post("/api/start", json!({}));
    assert_eq!(started.0, 200, "start: {}", started.1);
    assert_eq!(owner.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// The start guard's `authority_partial_state` is an OWNER's (authority.py:386-391): a create that
/// failed after preparing the seed (TLS directory unusable) leaves prepared state, and the Mac can
/// still become a target through `/api/join/request`, whose authority starts (app.py:1580-1584,
/// 1097-1122; REVIEW-laneS1R M8).
#[test]
fn packaged_failed_create_then_join_request_starts_the_target() {
    let state = state_root("failed-create-join");
    let owner = launch(&state);
    let seed = private(&state).join("seed-rs");
    std::fs::create_dir_all(&seed).unwrap();
    std::fs::write(seed.join("tls"), b"not a directory").unwrap();
    let created = owner.post("/api/create", json!({"consent": consent(4_000_000_000)}));
    assert_eq!(created.0, 409, "create: {}", created.1);
    let joined = owner.post(
        "/api/join/request",
        json!({"consent": consent(4_000_000_000)}),
    );
    assert_eq!(joined.0, 200, "join request: {}", joined.1);
    assert_eq!(
        joined.1["protocol"], "mycelium.pairing_request.v1",
        "{}",
        joined.1
    );
    let status = owner.status();
    assert_eq!(status["installation_role"], "target", "{status}");
    assert_eq!(status["authority"]["started"], true, "{status}");
    assert_eq!(owner.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&state).unwrap_or_default();
}
