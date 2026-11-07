// SPDX-License-Identifier: AGPL-3.0-only
//! Artifact grant / chunk request / chunk receipt outcomes and refusal codes equal what the
//! pinned v0.1.4 verifiers did over 630 mutated, correctly re-signed envelopes
//! (`round6-artifact-grant-probe.py`; scratch test keys only). The refusal `code` is what
//! the artifact source puts on the wire (`status:"refused"`) and what the recipient reports.
use base64::{Engine, engine::general_purpose::STANDARD};
use protocol::artifact_grant::{self as ag, GrantLimits};
use serde_json::Value;
use std::collections::BTreeMap;

fn limits() -> GrantLimits {
    GrantLimits {
        max_grant_ttl_ms: 21_600_000,
        max_request_ttl_ms: 120_000,
        request_ttl_ms: 60_000,
        clock_skew_ms: 30_000,
    }
}
fn expect(got: Result<(), &'static str>, want: &Value, what: &str) {
    match want["ok"].as_bool().unwrap() {
        true => assert_eq!(got, Ok(()), "{what}"),
        false => assert_eq!(got, Err(want["error"].as_str().unwrap()), "{what}"),
    }
}

#[test]
fn every_executed_oracle_grant_request_and_receipt_outcome_is_reproduced() {
    let v: Value = serde_json::from_str(include_str!(
        "../fixtures/round6-artifact-grant-vectors.json"
    ))
    .unwrap();
    let seed = &v["seed_key"];
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    let mut base: Option<(ag::ChunkRequest, ag::Grant)> = None;
    let data = STANDARD.decode(v["data_b64"].as_str().unwrap()).unwrap();
    for case in v["cases"].as_array().unwrap() {
        let kind = case["kind"].as_str().unwrap();
        let what = format!("{kind} {}", case["name"]);
        let ctx = &case["context"];
        let env = &case["envelope"];
        let got = match kind {
            "grant" => {
                let pin = if ctx["pin"] == "member" {
                    &v["member_key"]
                } else {
                    seed
                };
                ag::verify_grant(env, pin, ctx["now"].as_u64().unwrap(), limits())
                    .map(|_| ())
                    .map_err(|e| e.0)
            }
            "request" => {
                let sizes = ctx["sizes"].as_object().unwrap();
                let result = ag::verify_chunk_request(
                    env,
                    seed,
                    ctx["source_endpoint"].as_str().unwrap(),
                    ctx["source_digest"].as_str().unwrap(),
                    &|d| sizes.get(d).and_then(Value::as_u64),
                    ctx["now"].as_u64().unwrap(),
                    limits(),
                );
                if case["name"] == "base" {
                    base = Some(result.clone().unwrap());
                }
                result.map(|_| ()).map_err(|e| e.0)
            }
            _ => {
                let (request, grant) = base.as_ref().expect("base request precedes receipts");
                let bytes = STANDARD.decode(ctx["data_b64"].as_str().unwrap()).unwrap();
                ag::verify_chunk_receipt(env, request, grant, &bytes).map_err(|e| e.0)
            }
        };
        expect(got, &case["oracle"], &what);
        *counts.entry(kind.to_owned()).or_default() += 1;
    }
    assert_eq!(data.len(), 32);
    assert!(
        counts["grant"] > 300 && counts["request"] > 130 && counts["receipt"] > 130,
        "{counts:?}"
    );
}
