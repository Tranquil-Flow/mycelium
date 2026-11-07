// SPDX-License-Identifier: AGPL-3.0-only
use planner::compile::{assignment_id, compile_assignments};
use serde_json::{Value, json};
fn fixtures() -> Value {
    serde_json::from_str(include_str!("fixtures/assignments.json")).unwrap()
}
#[test]
fn pinned_python_assignments_are_byte_identity_compatible() {
    for row in fixtures().as_array().unwrap() {
        let assignments = compile_assignments(&row["input"]).unwrap();
        // Byte identity (the test's claim): `Value` equality would distinguish `1e-6` from
        // `1e-06` parse artefacts that canonical JSON does not.
        assert_eq!(
            planner::canonical(&assignments, false).unwrap(),
            planner::canonical(&row["expected"], false).unwrap()
        );
        for assignment in assignments.as_array().unwrap() {
            assert_eq!(
                assignment_id(assignment).unwrap(),
                assignment["assignment_id"]
            );
            assert_eq!(assignment["route_ready"], false);
        }
    }
}
#[test]
fn identity_changes_bind_every_runtime_relevant_assignment_field() {
    let f = fixtures();
    let a = &f[0]["expected"][0];
    for field in [
        "node_id",
        "artifact_cache_root",
        "manifest_digest",
        "model_id",
        "resolved_commit",
    ] {
        let mut b = a.clone();
        b[field] = json!(format!("{}-changed", b[field].as_str().unwrap()));
        assert_ne!(assignment_id(&b).unwrap(), a["assignment_id"]);
    }
    let mut b = a.clone();
    b["control_plane_binding"] = json!({"snapshot_generation":2});
    assert_ne!(assignment_id(&b).unwrap(), a["assignment_id"]);
}
#[test]
fn malformed_ranges_paths_identity_and_manifest_fail_before_output() {
    let f = fixtures();
    let original = &f[0]["input"];
    for mutation in [
        "digest",
        "path",
        "gap",
        "overlap",
        "bool_epoch",
        "runtime",
        "duplicate",
    ] {
        let mut v = original.clone();
        match mutation {
            "digest" => v["manifest"]["num_layers"] = json!(4),
            "path" => v["cache_roots"]["a"] = json!("/cache/../escape"),
            "gap" => v["route_plan"]["route"][1]["range"]["start_layer"] = json!(2),
            "overlap" => v["route_plan"]["route"][1]["range"]["start_layer"] = json!(0),
            "bool_epoch" => v["deployment_epoch"] = json!(true),
            "runtime" => v["runtime_by_node"]["a"]["dtype"] = json!("unsafe"),
            "duplicate" => v["route_plan"]["route"][1]["node_id"] = json!("a"),
            _ => unreachable!(),
        }
        assert!(compile_assignments(&v).is_err(), "{mutation}");
    }
}
#[test]
fn deployment_id_spellings_accepted_by_uuid_uuid_compile_to_the_canonical_assignments() {
    // v0.1.4 parses `deployment_id` with `uuid.UUID` and stores `str(namespace)`; the round-6
    // compiler refused the braced spelling (oracle vectors `deployment_id=...` in
    // fixtures/legacy-compile.json decide: accepted).
    let f = fixtures();
    let original = &f[0]["input"];
    let canonical = compile_assignments(original).unwrap();
    for spelling in [
        "{12345678-1234-5678-9234-567812345678}",
        "urn:uuid:12345678-1234-5678-9234-567812345678",
        "12345678123456789234567812345678",
        "12345678-1234-5678-9234-567812345678"
            .to_uppercase()
            .as_str(),
    ] {
        let mut v = original.clone();
        v["deployment_id"] = json!(spelling);
        assert_eq!(compile_assignments(&v).unwrap(), canonical, "{spelling}");
    }
}
