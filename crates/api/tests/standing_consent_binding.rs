// SPDX-License-Identifier: AGPL-3.0-only
//! A standing consent is scoped to one membership.
//!
//! A grant bound to an earlier swarm must not survive a new join. The member agent's gate compares
//! the grant's binding with the current membership and, on a mismatch, drops every pass silently:
//! no card, no report, no log. From the owner's side that is indistinguishable from a Mac that is
//! not there at all -- the run only ever said "1 of 2 Macs reported", with an empty report set and
//! no rejection reason.
use api::preferences::standing_consent::{self, DISCLOSURE_DIGEST};
use serde_json::json;
use std::fs;

fn root(tag: &str) -> std::path::PathBuf {
    let root = std::env::temp_dir().join(format!("mycelium-standing-{tag}-{}", std::process::id()));
    fs::remove_dir_all(&root).unwrap_or(());
    fs::create_dir_all(&root).unwrap();
    root
}

fn body() -> serde_json::Value {
    json!({
        "acknowledged": true,
        "disclosure_digest": DISCLOSURE_DIGEST,
        "caps": {
            "storage_bytes": 4_000_000_000u64,
            "max_memory_bytes": 4_000_000_000u64,
            "max_wall_seconds": 600,
            "max_commands": 1000,
        },
    })
}

fn policy() -> serde_json::Value {
    json!({
        "peer_visibility": true,
        "power": "ac_only",
        "storage_bytes": 4_000_000_000u64,
        "network": "unmetered_only",
        "background": false,
        "diagnostics": false,
        "thermal": "stop_on_serious",
    })
}

#[test]
fn a_grant_bound_to_an_earlier_swarm_does_not_authorize_a_new_join() {
    let root = root("stale");
    standing_consent::grant(&root, &body(), &policy(), 1_000.0, Some(16_000_000_000)).unwrap();
    let old =
        json!({"swarm_id": "sw-old", "node_id": "remote-mac-1", "seed_key_digest": "sha256:aa"});
    standing_consent::bind(&root, &old, 1_001.0).unwrap();
    assert!(
        standing_consent::for_binding(&root, &old)
            .unwrap()
            .is_some(),
        "the grant authorizes the membership it was bound to"
    );

    // A new join starts from a fresh grant: the stale bound record is dropped first.
    assert!(standing_consent::clear_bound(&root).unwrap());
    standing_consent::grant(&root, &body(), &policy(), 2_000.0, Some(16_000_000_000)).unwrap();
    let new =
        json!({"swarm_id": "sw-new", "node_id": "remote-mac-1", "seed_key_digest": "sha256:bb"});
    standing_consent::bind(&root, &new, 2_001.0).unwrap();

    assert!(
        standing_consent::for_binding(&root, &new)
            .unwrap()
            .is_some(),
        "the new membership is authorized"
    );
    assert!(
        standing_consent::for_binding(&root, &old)
            .unwrap()
            .is_none(),
        "the earlier swarm's binding no longer authorizes work"
    );
    fs::remove_dir_all(&root).unwrap_or(());
}
