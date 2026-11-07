// SPDX-License-Identifier: AGPL-3.0-only
//! Public constructor prerequisites for ManagedSessionFactory.
//! These tests compare owner_session outcomes; the real factory tests separately
//! exercise factory_open/current-binding, preserving their distinct semantics.
//! No private binding accessor, substitute factory, carrier or native success.
use super::*;

fn factory_inputs() -> Value {
    serde_json::from_str(include_str!(
        "../fixtures/managed-owner-factory-current-oracle.json"
    ))
    .expect("twice-captured r20 owner/factory/current-binding oracle")
}

fn replay_session_open(case: &Value) {
    let name = case["name"].as_str().expect("case name");
    let input = &case["input"];
    let r = Root::new();
    let inner = Arc::new(Mutex::new(Host::new(&r)));
    {
        let mut host = inner.lock().unwrap();
        host.member = input["current_member"].clone();
        host.wall.set(input["wall_seconds"].as_f64().unwrap());
        host.mono
            .set((input["monotonic_seconds"].as_f64().unwrap() * 1000.0) as u64);
        host.member_error = match input["current_member_error"].as_str() {
            None => None,
            Some("controlled_member_lookup_failed") => Some("controlled_member_lookup_failed"),
            Some(other) => panic!("unrepresented oracle callback error: {other}"),
        };
    }
    let owner = SharedManagedOwner::new(open(&r), host_factory(inner.clone(), None, false));
    let before = fs::read(r.0.join("owner.json")).unwrap();
    if input["owner_closed"] == true {
        owner.close().unwrap();
    }
    let timeout_seconds = input["timeout_seconds"].as_f64().unwrap();
    assert!(timeout_seconds.is_finite() && timeout_seconds > 0.0);
    let outcome = session_status(owner.session(
        input["node_id"].as_str().unwrap(),
        input["run_id"].as_str().unwrap(),
        input["deployment_id"].as_str().unwrap(),
        Span::from_millis((timeout_seconds * 1000.0) as u64),
    ));
    let (reads, io_count, nonce_count) = {
        let host = inner.lock().unwrap();
        (host.member_reads.clone(), host.calls.len(), host.nonce)
    };
    let expected_reads: Vec<Value> = case["member_reads"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|read| read[0] == "owner_session")
        .map(|read| read[1].clone())
        .collect();
    assert_eq!(
        json!(reads),
        json!(expected_reads),
        "{name}: fresh lookup order"
    );
    assert_eq!(io_count, 0, "{name}: construction must not send a request");
    assert_eq!(
        nonce_count, 0,
        "{name}: construction must not prepare a request"
    );
    assert_eq!(case["journal_unchanged"], true, "oracle scope");
    assert_eq!(
        fs::read(r.0.join("owner.json")).unwrap(),
        before,
        "{name}: no journal mutation"
    );
    let expected = if let Some(error) = case["owner_session"].get("error") {
        json!({"error": error})
    } else {
        assert!(case["owner_session"].get("ok").is_some(), "oracle outcome");
        json!({"ok": true})
    };
    assert_eq!(
        outcome, expected,
        "{name}: public owner.session admission must match the oracle"
    );
    owner.close().unwrap();
}

#[test]
fn public_session_open_matches_factory_prerequisite_oracle() {
    let fixture = factory_inputs();
    for case in fixture["cases"].as_array().unwrap() {
        if !case["name"].as_str().unwrap().starts_with("invalid_") {
            replay_session_open(case);
        }
    }
}

fn invalid_binding_case(name: &str) {
    let fixture = factory_inputs();
    let case = fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == name)
        .expect("captured invalid binding");
    replay_session_open(case);
}

// Executed behavioral RED in r20: target() omitted the public validator.
#[test]
fn public_session_open_rejects_invalid_generation_before_io() {
    invalid_binding_case("invalid_generation");
}

#[test]
fn public_session_open_rejects_invalid_endpoint_before_io() {
    invalid_binding_case("invalid_endpoint");
}
