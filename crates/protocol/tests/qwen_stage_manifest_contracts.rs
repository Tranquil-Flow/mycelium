// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 `qwen_stage_manifest` verifiers on cases recorded by executing the oracle
//! (`round8-qwen-manifest-probe.py`, scratch keys): the accepted output byte for byte, or the exact
//! code; a Python accident (TypeError/ValueError) must be a refusal.
use protocol::canonical;
use protocol::identity::Identity;
use protocol::qwen_stage_manifest as qm;
use serde_json::{Value, json};

fn signer(spec: &Value) -> Identity {
    Identity::from_secret(
        spec["endpoint"].as_str().unwrap(),
        &[u8::try_from(spec["byte"].as_u64().unwrap()).unwrap(); 32],
    )
    .unwrap()
}

/// The 600 kB `too_large` document is rebuilt here instead of stored (golden payload + the
/// oracle's recorded pack digest).
fn document_of(case: &Value, base: &Value) -> Value {
    if let Some(fill) = case.get("huge_golden_fill").and_then(Value::as_u64) {
        let mut doc = base.clone();
        doc["golden"] = json!({ "x": "y".repeat(usize::try_from(fill).unwrap()) });
        doc["pack_digest"] = case["document_pack_digest"].clone();
        doc
    } else {
        case["document"].clone()
    }
}

fn run(case: &Value, base: &Value) -> Result<Value, qm::StageManifestError> {
    match case["kind"].as_str().unwrap() {
        "verify" => qm::verify_stage_manifest(&document_of(case, base)),
        "digest" => qm::manifest_digest(&case["unsigned"]).map(Value::String),
        "sign" => qm::sign_stage_manifest(&case["manifest"], &signer(&case["signer"])),
        "verify_signed" => qm::verify_signed_stage_manifest(&case["envelope"], &case["pinned_key"]),
        "is_signed" => Ok(Value::Bool(qm::is_signed_stage_manifest(&case["document"]))),
        "hf_config" => qm::architecture_from_hf_config(&case["config"]),
        "build" => qm::build_stage_manifest(&case["parts"]),
        other => panic!("unknown kind {other}"),
    }
}

#[test]
fn stage_manifests_match_the_executed_oracle() {
    let v: Value = serde_json::from_str(include_str!(
        "../fixtures/round8-qwen-manifest-vectors.json"
    ))
    .unwrap();
    let cases = v["cases"].as_array().unwrap();
    let base = cases
        .iter()
        .find(|c| c["kind"] == "verify" && c["name"] == "valid")
        .map(|c| c["document"].clone())
        .unwrap();
    let (mut accepted, mut refused, mut accidents) = (0, 0, 0);
    for case in cases {
        let name = format!(
            "{}:{}",
            case["kind"].as_str().unwrap(),
            case["name"].as_str().unwrap()
        );
        let got = run(case, &base);
        if case.get("python_exception").is_some() {
            assert!(got.is_err(), "{name}: oracle crashed; v0.2 must refuse");
            accidents += 1;
        } else if case["accepted"] == true {
            let got = got.unwrap_or_else(|e| panic!("{name}: refused with {e}"));
            assert_eq!(
                String::from_utf8(canonical::to_bytes(&got).unwrap()).unwrap(),
                String::from_utf8(canonical::to_bytes(&case["result"]).unwrap()).unwrap(),
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
        (720, 70, 634, 16)
    );
}
