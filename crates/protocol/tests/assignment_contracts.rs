// SPDX-License-Identifier: AGPL-3.0-only
#[allow(dead_code)]
mod support;
use protocol::{assignment, canonical};
use serde_json::{Value, json};
fn run(v: &Value) -> Result<Value, String> {
    let required = match v["op"].as_str().unwrap() {
        "identity" | "assignment" => "assignment",
        "report" => "assignment report",
        "audit" => "route assignments reports",
        "audit_errors" => "route assignments reports audit",
        _ => unreachable!(),
    };
    for field in required.split_whitespace() {
        if v.get(field).is_none() {
            return Err(format!("missing input: {field}"));
        }
    }
    match v["op"].as_str().unwrap() {
        "identity" => {
            assignment::validate_identity(&v["assignment"])?;
            Ok(json!({"assignment_id": assignment::id_for(&v["assignment"])?}))
        }
        "assignment" => {
            assignment::validate(&v["assignment"])?;
            Ok(v["assignment"].clone())
        }
        "report" => assignment::report_errors(&v["assignment"], &v["report"]).map(|x| json!(x)),
        "audit" => assignment::audit(
            &v["route"],
            &v["assignments"],
            &v["reports"],
            "2026-01-01T00:00:00+00:00",
        ),
        "audit_errors" => {
            assignment::audit_errors(&v["route"], &v["assignments"], &v["reports"], &v["audit"])
                .map(|x| json!(x))
        }
        _ => unreachable!(),
    }
}
// O:contract:mycelium.layer_assignment.v2
// O:contract:mycelium.artifact_verification_report.v1
// O:contract:mycelium.provisioning_audit.v1
#[test]
fn assignment_report_and_audit_match_byte_exact_oracle() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/round3-assignment-vectors.json")).unwrap();
    for row in rows {
        let out = run(&row["value"]).unwrap();
        assert_eq!(
            canonical::to_bytes(&out).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes(),
            "{}",
            row["name"]
        );
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            let mut v = support::mutate(&row["value"], c);
            if let Some(id) = c.get("assignment_id_override") {
                v["assignment"]["assignment_id"] = id.clone();
            }
            let actual = run(&v);
            assert_eq!(
                actual.is_ok(),
                c["accepted"] == true,
                "{} #{i} {c}: {actual:?}",
                row["name"]
            );
            match actual {
                Ok(out) => assert_eq!(
                    canonical::to_bytes(&out).unwrap(),
                    c["canonical_utf8"].as_str().unwrap().as_bytes(),
                    "{} #{i} {c}: {out}",
                    row["name"]
                ),
                Err(e) if c["exception"] == "ValueError" => {
                    assert_eq!(e, c["error"].as_str().unwrap(), "{} #{i} {c}", row["name"])
                }
                Err(e) => assert!(!e.is_empty()),
            }
        }
    }
}
