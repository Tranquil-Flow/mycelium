// SPDX-License-Identifier: AGPL-3.0-only
//! The whole product snapshot of `ProductProjector.project` (`mycelium_product_spine/projector.py:308-1394`
//! @ f4e366e9), compared with the v0.1.4 projector's own output. `fixtures/oracle_product_snapshots.json`
//! was written by running that projector (pinned checkout, scratch driver recorded in the B13
//! qualification packet, its sha256 inside the file) over the protocol crate's contract fixtures
//! (`live-route-status-v1`, `route-qualification-v1`, `full-context-recovery-runtime-v1`) and labelled
//! member/assignment rows. The serving cases are test inputs: no Rust host produces a live route yet.
use api::{
    product_projector::{ProductProjector, ProjectionInput},
    wire::{Text, dumps_sorted},
};
use serde_json::{Map, Value};
use std::collections::BTreeMap;

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_product_snapshots.json")).expect("fixture")
}

fn salt(doc: &Value) -> Vec<u8> {
    let hex = doc["salt_hex"].as_str().expect("salt");
    (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).expect("hex"))
        .collect()
}

fn rows(value: &Value) -> Vec<Map<String, Value>> {
    value
        .as_array()
        .expect("rows")
        .iter()
        .map(|row| row.as_object().expect("row object").clone())
        .collect()
}

fn optional(value: &Value) -> Option<&Value> {
    (!value.is_null()).then_some(value)
}

#[test]
fn every_oracle_snapshot_is_reproduced_byte_for_byte() {
    let doc = fixture();
    let cases = doc["cases"].as_array().expect("cases");
    assert_eq!(cases.len(), 7, "every recorded case is replayed");
    let mut steps_run = 0;
    for case in cases {
        let name = case["case"].as_str().expect("name");
        let projector = ProductProjector::new(&salt(&doc)).expect("salt");
        if let Some(restore) = optional(&case["restore"]) {
            projector
                .restore_publication(
                    restore["generation"].as_u64().unwrap(),
                    restore["cursor"].as_u64().unwrap(),
                )
                .expect("restore");
        }
        for (index, step) in case["steps"].as_array().unwrap().iter().enumerate() {
            let members = rows(&step["members"]);
            let assignments = optional(&step["assignments"]).map(rows);
            let errors: BTreeMap<String, String> = step["source_errors"]
                .as_object()
                .unwrap()
                .iter()
                .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
                .collect();
            let input = ProjectionInput {
                members: &members,
                assignments: assignments.as_deref(),
                route_status: optional(&step["route_status"]),
                qualification: optional(&step["qualification"]),
                recovery_status: optional(&step["recovery_status"]),
                now_unix_ms: step["now_unix_ms"].as_u64().unwrap(),
                source_errors: &errors,
            };
            let got = projector
                .project(&input)
                .unwrap_or_else(|e| panic!("{name}[{index}]: {e:?}"));
            let expected = &step["expected"];
            let (got_bytes, expected_bytes) = (
                dumps_sorted(&got, Text::Ascii),
                dumps_sorted(expected, Text::Ascii),
            );
            if got_bytes != expected_bytes {
                for key in expected.as_object().unwrap().keys() {
                    assert_eq!(got[key], expected[key], "{name}[{index}] field {key}");
                }
                assert_eq!(got, *expected, "{name}[{index}]");
            }
            assert_eq!(got_bytes, expected_bytes, "{name}[{index}] bytes");
            protocol::product::snapshot(&got).expect("the snapshot is a valid product snapshot");
            steps_run += 1;
        }
    }
    assert_eq!(steps_run, 9);
}

#[test]
fn a_restored_publication_cannot_be_restored_twice_or_after_a_projection() {
    let projector = ProductProjector::new(&[7; 32]).unwrap();
    projector.restore_publication(5, 5).unwrap();
    assert!(projector.restore_publication(6, 6).is_err());
    let fresh = ProductProjector::new(&[7; 32]).unwrap();
    let errors = BTreeMap::new();
    fresh
        .project(&ProjectionInput {
            members: &[],
            assignments: None,
            route_status: None,
            qualification: None,
            recovery_status: None,
            now_unix_ms: 1,
            source_errors: &errors,
        })
        .unwrap();
    assert!(fresh.restore_publication(9, 9).is_err());
}

#[test]
fn an_unknown_source_error_is_refused() {
    let projector = ProductProjector::new(&[7; 32]).unwrap();
    for (source, reason) in [("planner-source", "x"), ("route-source", "Bad Code")] {
        let errors = BTreeMap::from([(source.to_owned(), reason.to_owned())]);
        let refused = projector.project(&ProjectionInput {
            members: &[],
            assignments: None,
            route_status: None,
            qualification: None,
            recovery_status: None,
            now_unix_ms: 1,
            source_errors: &errors,
        });
        assert!(refused.is_err(), "{source}: {reason}");
    }
}
