// SPDX-License-Identifier: AGPL-3.0-only
#[allow(dead_code)]
mod support;
use protocol::{ContractError, canonical, operator_projection as op};
use serde_json::Value;
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}
fn run(v: &Value) -> Result<Value, ContractError> {
    match v["op"].as_str().ok_or(ContractError("invalid_input"))? {
        "backup" => {
            if !truthy(v.get("owned").ok_or(ContractError("invalid_input"))?) {
                return Err(ContractError("seed_operator_backup_invalid"));
            }
            op::backup_manifest(
                v["raw"]
                    .as_str()
                    .ok_or(ContractError("invalid_input"))?
                    .as_bytes(),
            )
        }
        "revocation" => {
            for k in [
                "binding",
                "member",
                "expected_generation",
                "reason",
                "now",
                "node_id",
            ] {
                if v.get(k).is_none() {
                    return Err(ContractError("invalid_input"));
                }
            }
            op::seed_revocation_projection(
                &v["binding"],
                &v["member"],
                &v["expected_generation"],
                &v["reason"],
                &v["now"],
            )
        }
        "rotation" => op::seed_rotation_status(
            v.get("binding").ok_or(ContractError("invalid_input"))?,
            v.get("authority").ok_or(ContractError("invalid_input"))?,
            if v["authority"]["rotation"].is_null() {
                &Value::Null
            } else {
                v.get("acks").ok_or(ContractError("invalid_input"))?
            },
        ),
        _ => Err(ContractError("invalid_input")),
    }
}
// O:contract:mycelium.seed_backup.v1
// O:contract:mycelium.seed_operator_rotation.v1
// O:contract:mycelium.seed_operator_revocation.v1
#[test]
fn seed_backup_boundary_and_rotation_projection_match_oracle() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/round3-seed-vectors.json")).unwrap();
    for row in rows {
        assert_eq!(
            canonical::to_bytes(&run(&row["value"]).unwrap()).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            let result = run(&support::mutate(&row["value"], c));
            assert_eq!(
                result.is_ok(),
                c["accepted"] == true,
                "{} #{i} {c}: {result:?}",
                row["name"]
            );
            if let Ok(out) = result {
                assert_eq!(
                    canonical::to_bytes(&out).unwrap(),
                    c["canonical_utf8"].as_str().unwrap().as_bytes(),
                    "{} #{i} {c}: {out}",
                    row["name"]
                );
            }
        }
    }
}
