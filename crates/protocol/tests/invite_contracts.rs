// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 invite token / bundle verification on 214 cases recorded by executing the oracle
//! (`round7-invite-probe.py`, scratch keys): accept/refuse, exact code and accepted payload.
use protocol::invite::{self, InviteError};
use serde_json::Value;

#[test]
fn invite_tokens_and_bundles_match_the_executed_oracle() {
    let v: Value =
        serde_json::from_str(include_str!("../fixtures/round7-invite-vectors.json")).unwrap();
    let cases = v["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 214);
    let (mut accepted, mut refused, mut accidents) = (0, 0, 0);
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let now = case["now"].as_f64().unwrap();
        let (got_payload, got) = if case["kind"] == "token" {
            let records: Vec<Value> = case["key_records"].as_array().unwrap().clone();
            let r = invite::verify_invite(case["token"].as_str().unwrap(), &records, now);
            (r.as_ref().ok().cloned(), r.map(|_| ()))
        } else {
            let r = invite::verify_invite_bundle(&case["bundle"], now);
            let want = &case["result"];
            if let (Ok(b), true) = (&r, case["accepted"] == true) {
                assert_eq!(b.payload, want["payload"], "{name}");
                assert_eq!(
                    b.seed_key_digest,
                    want["seed_key_digest"].as_str().unwrap(),
                    "{name}"
                );
                assert_eq!(
                    &Value::Array(b.seed_key_records.clone()),
                    &want["seed_key_records"],
                    "{name}"
                );
            }
            (r.as_ref().ok().map(|b| b.payload.clone()), r.map(|_| ()))
        };
        if case.get("python_exception").is_some() {
            // A Python runtime accident (OverflowError), not an InviteError: v0.2 must refuse.
            assert!(got.is_err(), "{name}: oracle crashed; v0.2 must refuse");
            accidents += 1;
        } else if case["accepted"] == true {
            assert_eq!(got, Ok(()), "{name}");
            if case["kind"] == "token" {
                assert_eq!(got_payload.as_ref(), Some(&case["payload"]), "{name}");
            }
            accepted += 1;
        } else {
            assert_eq!(
                got,
                Err(InviteError(Box::leak(
                    case["code"].as_str().unwrap().to_owned().into_boxed_str()
                ))),
                "{name}"
            );
            refused += 1;
        }
    }
    assert_eq!((accepted, refused, accidents), (23, 190, 1));
}

#[test]
fn a_non_finite_now_is_refused_before_anything_else() {
    assert_eq!(
        invite::verify_invite("not a token", &[], f64::NAN),
        Err(InviteError("invite_time_invalid"))
    );
    assert_eq!(
        invite::verify_invite_bundle(&serde_json::json!({}), f64::INFINITY),
        Err(InviteError("invite_bundle_malformed"))
    );
}
