// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 `mycelium_platform_neutral.peer_profile` (the closed v2 membership declaration) on cases recorded
//! by executing the oracle (`round9-peer-profile-probe.py`, scratch keys): the accepted mapping, digest and
//! canonical length byte for byte, or the exact code; a Python accident must be a refusal.
use protocol::identity::Identity;
use protocol::peer_profile::{PeerProfile, sign_peer_profile, verify_peer_profile};
use serde_json::Value;

fn parse(document: &Value) -> Result<Value, &'static str> {
    let profile = PeerProfile::from_mapping(document).map_err(|e| e.0)?;
    let digest = profile.digest().map_err(|e| e.0)?;
    let canonical_len = profile.canonical_bytes().map_err(|e| e.0)?.len();
    Ok(serde_json::json!({
        "mapping": profile.to_mapping(),
        "digest": digest,
        "canonical_len": canonical_len,
        "is_unknown_platform": profile.is_unknown_platform(),
        "runtime_ids": profile.runtime_ids(),
    }))
}

#[test]
fn peer_profiles_match_the_executed_oracle() {
    let v: Value =
        serde_json::from_str(include_str!("../fixtures/round9-peer-profile-vectors.json")).unwrap();
    let (mut accepted, mut refused, mut accidents, mut skipped_now) = (0, 0, 0, 0);
    for case in v["cases"].as_array().unwrap() {
        let at = format!("{}:{}", case["kind"], case["name"]);
        let want = &case["result"];
        let got: Result<Value, &'static str> = match case["kind"].as_str().unwrap() {
            "parse" => parse(&case["document"]),
            "sign" => {
                let signer = Identity::from_secret(
                    case["signer"]["endpoint"].as_str().unwrap(),
                    &[u8::try_from(case["signer"]["byte"].as_u64().unwrap()).unwrap(); 32],
                )
                .unwrap();
                PeerProfile::from_mapping(&case["document"])
                    .and_then(|p| sign_peer_profile(&signer, &p))
                    .map(|envelope| serde_json::json!({ "envelope": envelope }))
                    .map_err(|e| e.0)
            }
            "verify" => match case["now"].as_f64() {
                // a non-number `now` cannot be expressed as an f64; the oracle refuses it too
                None => {
                    assert_eq!(want["code"], "peer_profile_now_invalid", "{at}");
                    skipped_now += 1;
                    continue;
                }
                Some(now) => verify_peer_profile(
                    &case["envelope"],
                    now,
                    case["expected_key_digest"].as_str(),
                    case["expected_node_id"].as_str(),
                )
                .map(|p| {
                    serde_json::json!({
                        "mapping": p.to_mapping(),
                        "digest": p.digest().unwrap(),
                    })
                })
                .map_err(|e| e.0),
            },
            other => panic!("{at}: unknown kind {other}"),
        };
        if want["accepted"] == true {
            let got = got.unwrap_or_else(|code| panic!("{at}: refused with {code}"));
            for key in [
                "digest",
                "canonical_len",
                "is_unknown_platform",
                "runtime_ids",
                "envelope",
            ] {
                if want.get(key).is_some() {
                    assert_eq!(got[key], want[key], "{at}: {key}");
                }
            }
            if !want["mapping"].is_null() {
                assert_eq!(got["mapping"], want["mapping"], "{at}: mapping");
            }
            accepted += 1;
        } else if let Some(code) = want["code"].as_str() {
            assert_eq!(got.map(|_| ()), Err(code), "{at}");
            refused += 1;
        } else {
            assert!(got.is_err(), "{at}: oracle crashed; v0.2 must refuse");
            accidents += 1;
        }
    }
    assert_eq!(
        (accepted, refused, accidents, skipped_now),
        (157, 881, 1, 5)
    );
}
