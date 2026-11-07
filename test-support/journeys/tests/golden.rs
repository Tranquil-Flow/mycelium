// SPDX-License-Identifier: AGPL-3.0-only
use journey_tests::compare;
use serde_json::{Value, json};
use std::collections::BTreeSet;
fn golden() -> Value {
    serde_json::from_str(include_str!("../fixtures/python-v014.json")).unwrap()
}
#[test]
fn captured_corpus_is_complete_unique_and_every_tag_exists_in_l0() {
    let fixture = golden();
    // Historical capture provenance stays immutable when the launch pin moves.
    assert_eq!(
        fixture["source_sha"],
        "f5bfb70981569c79cb873afe8e0c5be20342db7e"
    );
    assert_eq!(
        fixture["executable_sha256"],
        "af681b346ef301c7b36bcdfe2f89b5e71053c349f02d2445cd98bafe5fa3e69c"
    );
    assert_eq!(
        fixture["capture_kind"],
        "real_packaged_process_loopback_http"
    );
    let registry: Value =
        serde_json::from_str(include_str!("../../obligations/data/obligations.json")).unwrap();
    let registered = registry
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v["id"].as_str().unwrap())
        .collect::<BTreeSet<_>>();
    let expected = [
        "first_run",
        "onboarding_default_fields",
        "saved2_unjoined_target",
        "saved2_no_silent_upgrade",
        "saved2_restart_preserved",
        "missing_cookie_refused",
        "wrong_origin_refused",
        "missing_consent_refused",
        "valid_signed_request_preview",
        "tampered_signed_request_refused",
        "fresh4_owner_created",
        "owner_no_standing_member_grant",
        "stats_not_exposed_on_private_app",
        "choose_model_without_download",
        "fast_mode_saved",
        "model_selection_idle",
        "fast_restart_preserved",
        "normal_mode_saved",
        "normal_restart_preserved",
        "clean_teardown",
    ]
    .into_iter()
    .collect::<BTreeSet<_>>();
    let observations = fixture["observations"].as_array().unwrap();
    assert_eq!(observations.len(), expected.len());
    assert_eq!(
        observations
            .iter()
            .map(|v| v["id"].as_str().unwrap())
            .collect::<BTreeSet<_>>(),
        expected
    );
    for obs in observations {
        assert!(registered.contains(obs["obligation_id"].as_str().unwrap()));
        assert_eq!(obs.as_object().unwrap().len(), 6);
        for (field, value) in obs["fields"].as_object().unwrap() {
            assert!(
                !value.is_object() && !value.is_array(),
                "nested data escaped: {field}"
            );
            for forbidden in [
                "cookie",
                "launch_code",
                "private_key",
                "signature",
                "nonce",
                "fingerprint",
                "node_id",
            ] {
                assert!(
                    !field.contains(forbidden),
                    "sensitive field escaped: {field}"
                );
            }
        }
    }
}
#[test]
fn comparator_kills_every_captured_field_mutation_and_always_refuse() {
    let original = golden()["observations"].clone();
    for (index, obs) in original.as_array().unwrap().iter().enumerate() {
        let mut changed = original.clone();
        changed[index]["status"] = json!(599);
        assert!(compare(&original, &changed).is_err());
        for (field, value) in obs["fields"].as_object().unwrap() {
            let mut changed = original.clone();
            changed[index]["fields"][field] = if value.is_null() {
                json!(false)
            } else {
                Value::Null
            };
            assert!(
                compare(&original, &changed).is_err(),
                "survived:{index}:{field}"
            );
        }
    }
    let mut refused = original.clone();
    for obs in refused.as_array_mut().unwrap() {
        obs["status"] = json!(403);
    }
    assert!(compare(&original, &refused).is_err());
    assert!(compare(&original, &original).is_ok());
}
#[test]
fn cli_comparator_matches_real_fixture_without_starting_an_app() {
    let fixture = concat!(env!("CARGO_MANIFEST_DIR"), "/fixtures/python-v014.json");
    let result = std::process::Command::new(env!("CARGO_BIN_EXE_journey-tests"))
        .args(["compare", fixture, fixture])
        .output()
        .unwrap();
    assert!(result.status.success());
    assert!(
        String::from_utf8(result.stdout)
            .unwrap()
            .starts_with("PASS:observations_match:")
    );
}
