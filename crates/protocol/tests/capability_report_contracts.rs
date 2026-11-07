// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 `mycelium_platform_neutral.capability_report` (the signed per-family observation report) on cases
//! recorded by executing the oracle (`round9-capability-report-probe.py`, scratch keys): the accepted mapping,
//! digest and canonical length byte for byte, the freshness / unknown / gate-satisfaction projections, the
//! profile binding, the signed envelope, or the exact code; a Python accident must be a refusal.
use protocol::capability_report::{
    CapabilityReport, FamilyObservation, sign_capability_report, verify_capability_report,
};
use protocol::identity::Identity;
use protocol::peer_profile::PeerProfile;
use serde_json::{Map, Value, json};

const FAMILIES: [&str; 7] = [
    "memory",
    "storage",
    "limits",
    "thermal",
    "power",
    "lifecycle",
    "network",
];
const SOURCE_KINDS: [&str; 4] = [
    "native_observed",
    "synthetic_conformance",
    "operator_declared",
    "unknown",
];

fn parse(document: &Value, want: &Value, extras: bool) -> Result<Value, &'static str> {
    let report = CapabilityReport::from_mapping(document).map_err(|e| e.0)?;
    let digest = report.digest().map_err(|e| e.0)?;
    let canonical_len = report.canonical_bytes().map_err(|e| e.0)?.len();
    let mut out = json!({
        "mapping": report.to_mapping(),
        "digest": digest,
        "canonical_len": canonical_len,
    });
    if extras {
        // the oracle keys the instants by Python's `str(number)`; they are compared as numbers here
        let mut fresh = Map::new();
        for key in want["fresh"].as_object().unwrap().keys() {
            let at: f64 = key.parse().unwrap();
            fresh.insert(key.clone(), json!(report.is_fresh(at)));
        }
        let mut unknown = Map::new();
        let mut satisfies = Map::new();
        for family in FAMILIES {
            let observation: &FamilyObservation = report.family(family).unwrap();
            unknown.insert(family.to_owned(), json!(observation.is_unknown()));
            let mut by_kind = Map::new();
            for kind in SOURCE_KINDS {
                by_kind.insert(kind.to_owned(), json!(observation.satisfies(kind)));
            }
            satisfies.insert(family.to_owned(), Value::Object(by_kind));
        }
        out["fresh"] = Value::Object(fresh);
        out["unknown"] = Value::Object(unknown);
        out["satisfies"] = Value::Object(satisfies);
    }
    Ok(out)
}

#[test]
fn capability_reports_match_the_executed_oracle() {
    let v: Value = serde_json::from_str(include_str!(
        "../fixtures/round9-capability-report-vectors.json"
    ))
    .unwrap();
    let (mut accepted, mut refused, mut accidents, mut skipped_now) = (0, 0, 0, 0);
    for case in v["cases"].as_array().unwrap() {
        let at = format!("{}:{}", case["kind"], case["name"]);
        let want = &case["result"];
        let got: Result<Value, &'static str> = match case["kind"].as_str().unwrap() {
            "parse" => {
                let name = case["name"].as_str().unwrap();
                let extras = name == "base"
                    || name == "ints_as_numbers"
                    || name.starts_with("source_kind:")
                    || name.starts_with("unknown_all_null:");
                parse(&case["document"], want, extras)
            }
            "bound" => {
                let profile = PeerProfile::from_mapping(&case["profile"]).unwrap();
                CapabilityReport::from_mapping(&case["document"])
                    .and_then(|r| r.bound_to(&profile))
                    .map(|()| json!({}))
                    .map_err(|e| e.0)
            }
            "sign" => {
                let signer = Identity::from_secret(
                    case["signer"]["endpoint"].as_str().unwrap(),
                    &[u8::try_from(case["signer"]["byte"].as_u64().unwrap()).unwrap(); 32],
                )
                .unwrap();
                CapabilityReport::from_mapping(&case["document"])
                    .and_then(|r| sign_capability_report(&signer, &r))
                    .map(|envelope| json!({ "envelope": envelope }))
                    .map_err(|e| e.0)
            }
            "verify" => {
                // Python compares `now <= expires_at`, so a bool is the number 1 / 0; any other
                // non-number raises TypeError in the oracle and cannot be an f64 here.
                let now = match &case["now"] {
                    Value::Bool(b) => Some(f64::from(u8::from(*b))),
                    other => other.as_f64(),
                };
                match now {
                    None => {
                        assert!(want["python_exception"] == "TypeError", "{at}");
                        skipped_now += 1;
                        continue;
                    }
                    Some(now) => verify_capability_report(
                        &case["envelope"],
                        now,
                        case["expected_key_digest"].as_str(),
                    )
                    .map(|r| json!({ "mapping": r.to_mapping(), "digest": r.digest().unwrap() }))
                    .map_err(|e| e.0),
                }
            }
            other => panic!("{at}: unknown kind {other}"),
        };
        if want["accepted"] == true {
            let got = got.unwrap_or_else(|code| panic!("{at}: refused with {code}"));
            for key in [
                "digest",
                "canonical_len",
                "fresh",
                "unknown",
                "satisfies",
                "envelope",
            ] {
                if want.get(key).is_some() {
                    assert_eq!(got[key], want[key], "{at}: {key}");
                }
            }
            if let Some(mapping) = want.get("mapping") {
                assert_eq!(&got["mapping"], mapping, "{at}: mapping");
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
        (319, 1577, 6, 2)
    );
}
