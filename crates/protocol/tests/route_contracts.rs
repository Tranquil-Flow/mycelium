// SPDX-License-Identifier: AGPL-3.0-only
mod support;
use protocol::{canonical, route_contract};
use serde_json::Value;
#[test]
fn route_parser_and_unqualified_record_match_exact_oracle_outputs() {
    // O:contract:mycelium.route_plan.v2; O:contract:mycelium.route_qualification.v1.
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/remaining-route-vectors.json")).unwrap();
    for row in rows {
        let base = &row["value"];
        let run = |v: &Value| {
            if row["name"].as_str().unwrap().starts_with("plan") {
                route_contract::plan(v)
            } else {
                route_contract::unqualified(v)
            }
        };
        assert_eq!(
            canonical::to_bytes(&run(base).unwrap()).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            let v = support::mutate(base, c);
            let r = run(&v);
            assert_eq!(
                protocol::schemas::validate_as(base["protocol"].as_str().unwrap(), &v, 0).is_ok(),
                r.is_ok()
            );
            assert_eq!(
                r.is_ok(),
                c["accepted"] == true,
                "{} {i}: {c}; {r:?}",
                row["name"]
            );
            if let Err(ref e) = r
                && row["name"].as_str().unwrap().starts_with("qualification")
                && c["exception"] == "QualificationContractError"
            {
                assert_eq!(
                    e.0,
                    c["error"].as_str().unwrap().split(':').next().unwrap(),
                    "{} {i}: {c}",
                    row["name"]
                );
            }
            if let Ok(v) = r {
                assert_eq!(
                    canonical::to_bytes(&v).unwrap(),
                    c["canonical_utf8"].as_str().unwrap().as_bytes()
                );
            }
        }
        let mut positive = row.clone();
        positive["cases"] = serde_json::json!([]);
        positive["canonical_utf8"] = String::from_utf8(canonical::to_bytes(base).unwrap())
            .unwrap()
            .into();
        support::corpus(&serde_json::to_string(&[positive]).unwrap(), |_, v| {
            run(v).map(|_| ())
        });
    }
}
