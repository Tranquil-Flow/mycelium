// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 `android_onboarding` verifiers on cases recorded by executing the oracle
//! (`round8-android-onboarding-probe.py`, scratch keys, injected deterministic entropy):
//! accept/refuse, exact code and exact accepted output (handoff ciphertext included).
use protocol::android_onboarding::{self as ao, HandoffEntropy, OnboardingError};
use protocol::canonical;
use serde_json::Value;

fn hex32<const N: usize>(v: &Value) -> [u8; N] {
    hex::decode(v.as_str().unwrap())
        .unwrap()
        .try_into()
        .unwrap()
}

fn run(case: &Value) -> Result<Value, OnboardingError> {
    let now = case.get("now").and_then(Value::as_f64);
    match case["kind"].as_str().unwrap() {
        "quota" => ao::validate_android_quota(&case["quota"]),
        "request" => ao::verify_android_pairing_request(&case["request"], now.unwrap()),
        "approve" => {
            let e = &case["entropy"];
            let entropy = HandoffEntropy {
                sender_private: hex32(&e["sender_private"]),
                salt: hex32(&e["salt"]),
                aead_nonce: hex32(&e["aead_nonce"]),
            };
            ao::approve_android_pairing_request(
                &case["request"],
                &case["invite_bundle"],
                &case["quota"],
                now.unwrap(),
                &entropy,
            )
        }
        "decrypt" => ao::decrypt_android_invitation_handoff(
            &case["request"],
            &case["handoff"],
            &hex::decode(case["recipient_private_key"].as_str().unwrap()).unwrap(),
            now.unwrap(),
        ),
        other => panic!("unknown kind {other}"),
    }
}

#[test]
fn onboarding_verifiers_match_the_executed_oracle() {
    let v: Value = serde_json::from_str(include_str!(
        "../fixtures/round8-android-onboarding-vectors.json"
    ))
    .unwrap();
    let cases = v["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 1104);
    let (mut accepted, mut refused, mut accidents) = (0, 0, 0);
    for case in cases {
        let name = format!(
            "{}:{}",
            case["kind"].as_str().unwrap(),
            case["name"].as_str().unwrap()
        );
        let got = run(case);
        if case.get("python_exception").is_some() {
            // A Python runtime accident (OverflowError / ValueError): v0.2 must refuse.
            assert!(got.is_err(), "{name}: oracle crashed; v0.2 must refuse");
            accidents += 1;
        } else if case["accepted"] == true {
            let got = got.unwrap_or_else(|e| panic!("{name}: refused with {e}"));
            assert_eq!(
                canonical::to_bytes(&got).unwrap(),
                canonical::to_bytes(&case["result"]).unwrap(),
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
    assert_eq!((accepted, refused, accidents), (99, 995, 10));
}
