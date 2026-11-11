// SPDX-License-Identifier: AGPL-3.0-only
//! The owner-pin field of the owner-signed run announcement (PH3B U2, slice S4b): an ADDITIVE,
//! versioned key `owner_control` that carries what `shard_member --owner-control` carries (the
//! owner's member-control endpoint key, never an address). Contract: plans/S4B-PLAN.md section 0.
//! Model-free; the signed read response is produced by the real `SignedRoutes` callback.
use api::{
    run_exchange::{Announce, RunExchange},
    run_exchange_routes::{PATH_READ, RESPONSE, SignedRoutes},
    wire::Text,
};
use protocol::{TimeLimits, identity::Identity, managed};
use serde_json::{Value, json};

fn oracle() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_signed_exchange.json")).unwrap()
}
/// The fixture's owner exchange (`signed_exchange.rs`): announcement `collect`, run-1 / dep-1.
fn owner_exchange(fixture: &Value) -> (RunExchange, String, f64) {
    let row = &fixture["rows"][0];
    let now = row["now"].as_f64().unwrap();
    let exchange = RunExchange::new(Box::new(move || now));
    let node = row["member"]["node_id"].as_str().unwrap().to_owned();
    exchange.announce(Announce { phase:"collect".into(),run_id:Some("run-1".into()),deployment_id:Some("dep-1".into()),per_node:json!({node.clone():{"bootstrap_offer_id":"offer-one"},"other-node":{"bootstrap_offer_id":"offer-secret"}}).as_object().unwrap().clone(),..Default::default() }).unwrap();
    (exchange, node, now)
}
fn owner_signer() -> Identity {
    Identity::from_secret("owner-fixed", &[7; 32]).unwrap()
}
fn limits() -> TimeLimits {
    TimeLimits {
        max_ttl_seconds: 300.0,
        clock_skew_seconds: 30.0,
    }
}
/// The seed-signed read response for the fixture's member (real `SignedRoutes::dispatch_guarded`).
fn signed_read(exchange: &RunExchange, fixture: &Value, now: f64) -> Value {
    let signer = owner_signer();
    let observe = |_: &str, _: f64, _: Option<f64>| {};
    let routes = SignedRoutes {
        exchange,
        signer: &signer,
        swarm_id: fixture["swarm_id"].as_str().unwrap(),
        owner_node_id: fixture["owner_node_id"].as_str().unwrap(),
        observe_clock: &observe,
        response_ttl: budgets::Span::from_millis(60_000),
    };
    let row = &fixture["rows"][0];
    assert_eq!(row["path"], PATH_READ);
    routes
        .dispatch_guarded(PATH_READ, &row["envelope"], &row["member"], now)
        .unwrap()
}

/// Existing bytes stay: an owner that never sets the pin answers the executed v0.1.4 oracle's
/// announcement exactly (same keys, same canonical bytes once the per-process incarnation is
/// set aside, as `signed_exchange.rs` does), in the same insertion order, under a valid
/// signature, and the response statement keeps its closed key set.
#[test]
fn an_announcement_without_the_pin_keeps_the_oracle_bytes_and_a_valid_signature() {
    let fixture = oracle();
    let (exchange, node, now) = owner_exchange(&fixture);
    let response = signed_read(&exchange, &fixture, now);
    let signer = owner_signer();
    let statement = managed::verify(
        &response,
        RESPONSE,
        &signer.public_key_record(),
        signer.endpoint_id(),
        now,
        limits(),
    )
    .expect("the response signature stays valid");
    let mut announcement = statement["announcement"].clone();
    let incarnation = announcement
        .as_object_mut()
        .unwrap()
        .remove("owner_incarnation")
        .unwrap();
    let expected = &fixture["rows"][0]["expected"]["ok"];
    assert_eq!(announcement, expected["announcement"]);
    assert_eq!(
        protocol::canonical::to_bytes(&announcement).unwrap(),
        protocol::canonical::to_bytes(&expected["announcement"]).unwrap(),
        "canonical announcement bytes"
    );
    let keys = |v: &Value| v.as_object().unwrap().keys().cloned().collect::<Vec<_>>();
    assert_eq!(keys(&statement), keys(expected), "closed response keys");
    // The owner's own serialisation (insertion order, `json.dumps` text), pinned at base 981c025.
    let dumped = String::from_utf8(exchange.announcement(Some(&node)).dumps(Text::Ascii))
        .unwrap()
        .replace(incarnation.as_str().unwrap(), "owner-process-INCARNATION");
    assert_eq!(
        dumped,
        r#"{"protocol":"mycelium.a13_demo_run_announcement.v1","phase":"collect","run_id":"run-1","deployment_id":"dep-1","model":null,"slice_mode":false,"manifest_digest":null,"work_limits":null,"issued_at":1793267919.837374,"owner_incarnation":"owner-process-INCARNATION","bootstrap_offer_id":"offer-one","node_slice_plan":null,"member_resources":null,"model_source":null}"#
    );
}
