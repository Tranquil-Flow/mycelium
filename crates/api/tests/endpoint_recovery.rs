// SPDX-License-Identifier: AGPL-3.0-only
//! Lane B, B10: the recovery card's own checks (`endpoint_recovery.py` @ f4e366e9,
//! `endpoint_card` :12-18, `verify_endpoint_card` :21-36, `refresh_endpoint_card` :117-142),
//! below the packaged routes `crates/node-service/tests/packaged_recovery.rs` drives. Each refusal
//! is the source's `ValueError`, which the handler answers `action_not_completed`.
use api::endpoint_recovery::{endpoint_card, refresh_endpoint_card, verify_endpoint_card};
use protocol::identity::Identity;
use serde_json::{Map, Value, json};

const OLD: &str = "https://old.example.org";
const NEW: &str = "https://new.example.org";

fn seed() -> Identity {
    Identity::from_secret("test-seed", &[7; 32]).unwrap()
}
fn card_at(signer: &Identity, now: f64) -> Value {
    endpoint_card(signer, "swarm-1", "seed-1", &json!(OLD), &json!(NEW), now).unwrap()
}
fn membership(signer: &Identity, seed_url: &str) -> Map<String, Value> {
    json!({
        "swarm_id": "swarm-1", "seed_node_id": "seed-1", "seed_url": seed_url,
        "seed_key_digest": signer.verification_key_digest(),
        "seed_key_records": [signer.public_key_record()],
    })
    .as_object()
    .unwrap()
    .clone()
}
fn refused(card: &Value, membership: &Map<String, Value>, now: f64) -> bool {
    verify_endpoint_card(card, membership, now)
        .is_err_and(|e| e.status == 409 && e.code == "action_not_completed")
}

#[test]
fn the_seeds_own_card_moves_its_membership_to_the_new_origin_within_its_window() {
    let signer = seed();
    let card = card_at(&signer, 1000.0);
    assert_eq!(card["statement"]["expires_at"], json!(1300.0));
    for (url, now) in [(OLD, 1000.0), (OLD, 970.0), (OLD, 1299.5), (NEW, 1000.0)] {
        assert_eq!(
            verify_endpoint_card(&card, &membership(&signer, url), now).map_err(|e| e.code),
            Ok(NEW.to_owned()),
            "{url} at {now}"
        );
    }
    // Too early (more than 30 s before its issue), expired, another address.
    let m = membership(&signer, OLD);
    assert!(refused(&card, &m, 969.0));
    assert!(refused(&card, &m, 1300.0));
    assert!(refused(
        &card,
        &membership(&signer, "https://other.example.org"),
        1000.0
    ));
    // `endpoint_unchanged`.
    assert!(endpoint_card(&signer, "s", "n", &json!(OLD), &json!(OLD), 1.0).is_err());
}

#[test]
fn a_card_that_is_not_the_seeds_own_untouched_statement_is_refused() {
    let signer = seed();
    let m = membership(&signer, OLD);
    let card = card_at(&signer, 1000.0);
    // Another statement under this signature (the digest it signed no longer matches).
    let mut moved = card.clone();
    moved["statement"]["public_origin"] = json!("https://elsewhere.example.org");
    assert!(refused(&moved, &m, 1000.0));
    // This statement under another card's signature (same key, other statement).
    let mut swapped = card.clone();
    swapped["signature"] = card_at(&signer, 1001.0)["signature"].clone();
    assert!(refused(&swapped, &m, 1000.0));
    // A forged signature value with every other field intact.
    let mut forged = card.clone();
    let other = Identity::from_secret("test-seed", &[9; 32]).unwrap();
    let theirs = other.sign(&card["statement"]).unwrap();
    forged["signature"]["signature"] = theirs["signature"].clone();
    assert!(refused(&forged, &m, 1000.0));
    // Another seed's own card, and this card under another swarm's membership.
    assert!(refused(&card_at(&other, 1000.0), &m, 1000.0));
    let mut swarm = m.clone();
    swarm.insert("swarm_id".into(), json!("swarm-2"));
    assert!(refused(&card, &swarm, 1000.0));
    // Shape: an extra field, a bool time, an over-long lifetime.
    let mut extra = card.clone();
    extra["statement"]["note"] = json!("x");
    assert!(refused(&extra, &m, 1000.0));
    let mut outer = card.clone();
    outer["extra"] = json!(1);
    assert!(refused(&outer, &m, 1000.0));
}

#[test]
fn the_owner_reissues_its_kept_card_for_the_origin_it_answers_on_now() {
    let signer = seed();
    let old = card_at(&signer, 1000.0);
    let fresh =
        refresh_endpoint_card(&signer, "swarm-1", "seed-1", NEW, &old, None, 5000.0).unwrap();
    let s = &fresh["statement"];
    assert_eq!(
        (
            &s["previous_origin"],
            &s["public_origin"],
            &s["issued_at"],
            &s["expires_at"]
        ),
        (&json!(OLD), &json!(NEW), &json!(5000.0), &json!(5300.0))
    );
    assert!(verify_endpoint_card(&fresh, &membership(&signer, OLD), 5000.0).is_ok());
    // The owner names another old origin; it must be canonical text (`canonical_https_origin`
    // refuses any other spelling).
    let named = |origin: &str| {
        refresh_endpoint_card(
            &signer,
            "swarm-1",
            "seed-1",
            NEW,
            &old,
            Some(&json!(origin)),
            5000.0,
        )
        .map(|card| card["statement"]["previous_origin"].clone())
        .map_err(|e| e.code)
    };
    assert_eq!(
        named("https://older.example.org"),
        Ok(json!("https://older.example.org"))
    );
    assert_eq!(
        named("https://Older.example.org"),
        Err("action_not_completed")
    );
    // The seed answers on another origin now (`endpoint_recovery_origin_changed`), and a kept card
    // that is not this seed's.
    assert!(
        refresh_endpoint_card(
            &signer,
            "swarm-1",
            "seed-1",
            "https://third.example.org",
            &old,
            None,
            5000.0
        )
        .is_err()
    );
    let other = Identity::from_secret("test-seed", &[9; 32]).unwrap();
    assert!(
        refresh_endpoint_card(
            &signer,
            "swarm-1",
            "seed-1",
            NEW,
            &card_at(&other, 1000.0),
            None,
            5000.0
        )
        .is_err()
    );
}
