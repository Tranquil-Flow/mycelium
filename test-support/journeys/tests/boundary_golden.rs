// SPDX-License-Identifier: AGPL-3.0-only
use journey_tests::compare;
use serde_json::{Value, json};
use std::collections::BTreeSet;

fn fixture() -> Value {
    serde_json::from_str(include_str!("../fixtures/python-v014-boundaries.json")).unwrap()
}

#[test]
fn boundary_fixture_is_final_oracle_complete_and_registry_bound() {
    let captured = fixture();
    // Preserve historical capture provenance; the launch admission pin can move
    // only independently. Replaying a new target must not rewrite old evidence.
    assert_eq!(
        captured["source_sha"],
        "ef9ae61de0222c78d83c8133f8f709619394bf51"
    );
    assert_eq!(
        captured["executable_sha256"],
        "bc06dc664353c5dd15b937afd8e5a5f464131c5248fd906a325a642c37e85467"
    );
    assert_eq!(
        captured["capture_kind"],
        "real_packaged_process_loopback_http"
    );
    let expected = [
        "boundary_owner_created",
        "boundary_member_request",
        "simultaneous_owner_status",
        "simultaneous_unjoined_member_status",
        "two_live_isolated_instances",
        "join_window_waits_for_public_relay",
        "pending_window_status",
        "pending_window_revoked",
        "revoked_window_absent",
        "external_loopback_relay_refused",
        "relay_refusal_keeps_owner_private",
        "manual_pairing_handoff_issued",
        "manual_pairing_approval_idempotent",
        "manual_pairing_quota_change_refused",
        "handoff_request_binding_refused",
        "manual_join_loopback_tls_untrusted",
        "failed_manual_join_is_not_membership",
        "failed_manual_join_did_not_enroll_member",
        "saved2_after_failed_join_no_upgrade",
        "unjoined_standing_withdraw_missing",
        "unjoined_window_poll_missing",
        "unjoined_window_cancel",
        "member_private_stats_not_exposed",
        "no_model_no_execution_proposal",
        "owner_cannot_leave_as_member",
        "leave_requires_confirmation",
        "leave_rejects_unknown_mode",
        "unjoined_instant_leave_complete",
        "unjoined_instant_leave_status",
        "unjoined_leave_restart_preserved",
        "unjoined_smooth_leave_complete",
        "unjoined_smooth_leave_status",
        "boundary_clean_teardown",
    ];
    let observations = captured["observations"].as_array().unwrap();
    assert_eq!(observations.len(), expected.len());
    assert_eq!(
        observations
            .iter()
            .map(|v| v["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        expected
    );
    let registry: Value =
        serde_json::from_str(include_str!("../../obligations/data/obligations.json")).unwrap();
    let registered: BTreeSet<_> = registry
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v["id"].as_str().unwrap())
        .collect();
    for row in observations {
        assert!(registered.contains(row["obligation_id"].as_str().unwrap()));
        assert_eq!(row.as_object().unwrap().len(), 6);
        for (field, value) in row["fields"].as_object().unwrap() {
            assert!(!value.is_object() && !value.is_array());
            for secret in [
                "cookie",
                "launch_code",
                "private_key",
                "signature",
                "nonce",
                "fingerprint",
                "node_id",
                "ciphertext",
                "request_digest",
                "recipient_key",
            ] {
                assert!(!field.contains(secret), "private field: {field}");
            }
        }
    }
}

#[test]
fn comparator_rejects_every_boundary_field_status_and_population_mutation() {
    let original = fixture()["observations"].clone();
    assert!(compare(&original, &original).is_ok());
    for (index, row) in original.as_array().unwrap().iter().enumerate() {
        let mut changed = original.clone();
        changed[index]["status"] = json!(599);
        assert!(compare(&original, &changed).is_err());
        let mut missing = original.clone();
        missing.as_array_mut().unwrap().remove(index);
        assert!(compare(&original, &missing).is_err());
        for (key, value) in row["fields"].as_object().unwrap() {
            let mut changed = original.clone();
            changed[index]["fields"][key] = if value.is_null() {
                json!(false)
            } else {
                Value::Null
            };
            assert!(
                compare(&original, &changed).is_err(),
                "survived: {index}:{key}"
            );
        }
    }
    let mut extra = original.clone();
    extra.as_array_mut().unwrap().push(original[0].clone());
    assert!(compare(&original, &extra).is_err());
    let mut refused = original.clone();
    for row in refused.as_array_mut().unwrap() {
        row["status"] = json!(403);
    }
    assert!(compare(&original, &refused).is_err());
    // A TLS refusal fixture is never interchangeable with the original baseline.
    let baseline: Value =
        serde_json::from_str(include_str!("../fixtures/python-v014.json")).unwrap();
    assert!(compare(&baseline["observations"], &original).is_err());
}
