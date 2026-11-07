// SPDX-License-Identifier: AGPL-3.0-only
#[allow(dead_code)]
mod support;
use protocol::{ContractError, canonical, operator_projection as op};
use serde_json::Value;
fn run(v: &Value) -> Result<Value, ContractError> {
    let required = match v["op"].as_str().unwrap() {
        "governance" => "ledger_utf8 manifest_utf8 gate revision status now",
        "inventory" => "binding members now",
        "incidents" => "deployment_id events now",
        _ => unreachable!(),
    };
    for key in required.split_whitespace() {
        if v.get(key).is_none() {
            return Err(ContractError("missing_input"));
        }
    }
    match v["op"].as_str().unwrap() {
        "governance" => {
            fn raw(v: &Value, field: &str) -> Result<Vec<u8>, ContractError> {
                if let Some(encoded) = v.get(format!("{field}_hex")) {
                    return hex::decode(encoded.as_str().ok_or(ContractError("invalid_input"))?)
                        .map_err(|_| ContractError("invalid_input"));
                }
                Ok(v[format!("{field}_utf8")]
                    .as_str()
                    .ok_or(ContractError("invalid_input"))?
                    .as_bytes()
                    .to_vec())
            }
            op::governance(&raw(v, "ledger")?, &raw(v, "manifest")?, v)
        }
        "inventory" => op::seed_inventory(&v["binding"], &v["members"], &v["now"]),
        "incidents" => op::incidents(&v["deployment_id"], &v["events"], &v["now"], 64, 128),
        _ => unreachable!(),
    }
}
// O:contract:mycelium.governance_readiness.v1
// O:contract:mycelium.seed_operator_inventory.v1
// O:contract:mycelium.live_route_incident.v1
#[test]
fn source_projections_match_positive_bytes_and_negative_vectors() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/round3-projection-vectors.json")).unwrap();
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
