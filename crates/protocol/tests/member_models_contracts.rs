// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 `mycelium_member_models` on cases recorded by executing the oracle
//! (`round8-member-models-probe.py`, scratch keys): accept/refuse, exact code and exact output.
use protocol::canonical;
use protocol::identity::Identity;
use protocol::member_models::{self as mm, LocalModel, MemberModelError};
use serde_json::{Value, json};

fn run(case: &Value) -> Result<Value, MemberModelError> {
    match case["kind"].as_str().unwrap() {
        "projection" => mm::inventory_entry_from_catalog_projection(&case["projection"]),
        "create" => {
            let s = &case["signer"];
            let signer = Identity::from_secret(
                s["endpoint"].as_str().unwrap(),
                &[u8::try_from(s["byte"].as_u64().unwrap()).unwrap(); 32],
            )
            .unwrap();
            mm::create_member_model_inventory(
                &case["member_id"],
                &case["membership_generation"],
                case["entries"].as_array().unwrap(),
                &case["observed_at_unix_ms"],
                &case["valid_until_unix_ms"],
                &signer,
            )
        }
        "validate" => mm::validate_member_model_inventory(
            &case["envelope"],
            &case["authority"],
            case["now_unix_ms"].as_i64().unwrap(),
        ),
        "authorities" => mm::validate_member_authorities(case["authorities"].as_array().unwrap())
            .map(|m| Value::Object(m.into_iter().collect())),
        "reconcile" => {
            let local: Vec<LocalModel> = case["local_entries"]
                .as_array()
                .unwrap()
                .iter()
                .map(|e| LocalModel {
                    model_id: e["model_id"].as_str().unwrap().to_owned(),
                    revision: e["revision"].as_str().unwrap().to_owned(),
                    projection: e["projection"].clone(),
                })
                .collect();
            mm::reconcile_member_discovery(
                &local,
                case["inventories"].as_array().unwrap(),
                case["authorities"].as_array().unwrap(),
                case["now_unix_ms"].as_i64().unwrap(),
            )
            .map(|r| {
                json!({
                    "discovered_entries": r.discovered_entries,
                    "discovery": r.discovery,
                    "entry_discovery": r.entry_discovery.into_iter()
                        .map(|((id, rev), meta)| json!([[id, rev], meta])).collect::<Vec<_>>(),
                })
            })
        }
        other => panic!("unknown kind {other}"),
    }
}

/// What the oracle's recording `catalog_document` stand-in saw, reduced to the fields this
/// module owns (the local entry list and generation are inputs echoed back).
fn expected(case: &Value) -> Value {
    if case["kind"] == "reconcile" {
        let r = &case["result"];
        json!({
            "discovered_entries": r["discovered_entries"],
            "discovery": r["discovery"],
            "entry_discovery": r["entry_discovery"],
        })
    } else {
        case["result"].clone()
    }
}

#[test]
fn member_models_match_the_executed_oracle() {
    let v: Value = serde_json::from_str(include_str!(
        "../fixtures/round8-member-models-vectors.json"
    ))
    .unwrap();
    let cases = v["cases"].as_array().unwrap();
    let (mut accepted, mut refused, mut accidents) = (0, 0, 0);
    for case in cases {
        let name = format!(
            "{}:{}",
            case["kind"].as_str().unwrap(),
            case["name"].as_str().unwrap()
        );
        let got = run(case);
        if case.get("python_exception").is_some() {
            assert!(got.is_err(), "{name}: oracle crashed; v0.2 must refuse");
            accidents += 1;
        } else if case["accepted"] == true {
            let got = got.unwrap_or_else(|e| panic!("{name}: refused with {e}"));
            assert_eq!(
                canonical::to_bytes(&got)
                    .map(String::from_utf8)
                    .unwrap()
                    .unwrap(),
                canonical::to_bytes(&expected(case))
                    .map(String::from_utf8)
                    .unwrap()
                    .unwrap(),
                "{name}"
            );
            accepted += 1;
        } else {
            assert_eq!(
                got.map(|_| ()).map_err(|e| e.0),
                Err(case["code"].as_str().unwrap()),
                "{name}"
            );
            refused += 1;
        }
    }
    assert_eq!(
        (cases.len(), accepted, refused, accidents),
        (889, 178, 676, 35)
    );
}
