// SPDX-License-Identifier: AGPL-3.0-only
mod support;
use protocol::{ContractError, canonical, gossip};
use serde_json::Value;
// Oracle wire hard cap, not an operational expiry default. TODO(L1): typed ID.
const LIMITS: gossip::Limits = gossip::Limits {
    max_record_ttl_ms: 604_800_000,
};
fn corpus(raw: &str, f: impl Fn(&Value) -> Result<Value, ContractError>) {
    let rows: Vec<Value> = serde_json::from_str(raw).unwrap();
    assert!(!rows.is_empty());
    for row in rows {
        let base = &row["value"];
        assert_eq!(
            canonical::to_bytes(&f(base).unwrap()).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes(),
            "{}",
            row["name"]
        );
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            let mut v = support::mutate(base, c);
            if c["rehash_records"] == true
                && let Some(records) = v.get_mut("records").and_then(Value::as_array_mut)
            {
                for r in records {
                    if r.is_object() && r["payload"].is_object() {
                        r["payload_hash"] = canonical::digest(&r["payload"]).unwrap().into();
                    }
                }
            }
            let result = f(&v);
            assert_eq!(
                result.is_ok(),
                c["accepted"] == true,
                "{} case {i}: {c}; {result:?}",
                row["name"]
            );
            match result {
                Ok(v) => assert_eq!(
                    canonical::to_bytes(&v).unwrap(),
                    c["canonical_utf8"].as_str().unwrap().as_bytes(),
                    "{} case {i}",
                    row["name"]
                ),
                Err(e) => assert!(!e.0.is_empty()),
            }
        }
        let mut positive = row.clone();
        positive["canonical_utf8"] = String::from_utf8(canonical::to_bytes(base).unwrap())
            .unwrap()
            .into();
        positive["cases"] = serde_json::json!([]);
        support::corpus(&serde_json::to_string(&[positive]).unwrap(), |_, v| {
            f(v).map(|_| ())
        });
    }
}
#[test]
fn gossip_views_and_bundle_derive_exact_bytes_and_refuse_invalid_sources() {
    // O:contract:mycelium.gossip.router_view.v1; O:contract:mycelium.gossip.allocator_view.v1; O:contract:mycelium.gossip.evidence_bundle.v1.
    corpus(
        concat!(include_str!("../fixtures/remaining-gossip-derive-vectors.json.part-00"), include_str!("../fixtures/remaining-gossip-derive-vectors.json.part-01")),
        |v| gossip::build_bundle(v, LIMITS),
    );
}
#[test]
fn gossip_bundle_rejects_forged_views_and_digests() {
    // O:contract:mycelium.gossip.evidence_bundle.v1.
    corpus(
        include_str!("../fixtures/remaining-gossip-bundle-vectors.json"),
        |v| gossip::parse_bundle(v, LIMITS),
    );
}
#[test]
fn gossip_bundle_refusals_carry_the_oracle_per_field_text() {
    // O:contract:mycelium.gossip.evidence_bundle.v1: `evidence_bundle_from_dict` raises a
    // distinct EvidenceBundleError text per failed field; planner/assignment callers observe it.
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../fixtures/remaining-gossip-bundle-vectors.json"
    ))
    .unwrap();
    let (mut refused, mut mismatched) = (0, std::collections::BTreeMap::new());
    for row in rows {
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            if c["accepted"] == true {
                continue;
            }
            let mut v = support::mutate(&row["value"], c);
            if c["rehash_records"] == true
                && let Some(records) = v.get_mut("records").and_then(Value::as_array_mut)
            {
                for r in records {
                    if r.is_object() && r["payload"].is_object() {
                        r["payload_hash"] = canonical::digest(&r["payload"]).unwrap().into();
                    }
                }
            }
            refused += 1;
            let want = c["error"].as_str().unwrap();
            let text = match gossip::parse_bundle(&v, LIMITS) {
                Ok(_) => panic!("case {i}: oracle refused {want:?}; Rust accepted"),
                Err(e) => e.0,
            };
            if text != want {
                *mismatched
                    .entry(format!("{want:?} <- Rust {text:?}"))
                    .or_insert(0usize) += 1;
            }
        }
    }
    assert_eq!(refused, 9_459);
    assert!(mismatched.is_empty(), "{mismatched:#?}");
}
#[test]
fn gossip_derivation_refusals_carry_the_oracle_evidence_text() {
    // O:contract:mycelium.gossip.evidence_bundle.v1 via the builder path: every oracle
    // `EvidenceBundleError` has the same text; harness-level shape errors are only refusals.
    let rows: Vec<Value> = serde_json::from_str(concat!(include_str!("../fixtures/remaining-gossip-derive-vectors.json.part-00"), include_str!("../fixtures/remaining-gossip-derive-vectors.json.part-01")))
    .unwrap();
    let (mut compared, mut refusals, mut container_artifacts) = (0, 0, 0);
    let mut mismatched = std::collections::BTreeMap::new();
    for row in rows {
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            if c["accepted"] == true {
                continue;
            }
            let mut v = support::mutate(&row["value"], c);
            if c["rehash_records"] == true
                && let Some(records) = v.get_mut("records").and_then(Value::as_array_mut)
            {
                for r in records {
                    if r.is_object() && r["payload"].is_object() {
                        r["payload_hash"] = canonical::digest(&r["payload"]).unwrap().into();
                    }
                }
            }
            let result = gossip::build_bundle(&v, LIMITS);
            let Err(e) = result else {
                panic!("case {i}: oracle refused {c}; Rust accepted")
            };
            refusals += 1;
            if c["exception"] != "EvidenceBundleError" {
                continue;
            }
            compared += 1;
            let want = c["error"].as_str().unwrap();
            // `build_evidence_bundle` takes Python iterables: a JSON object/string container is
            // iterated element-wise (and each element fails the per-item shape check) whereas
            // `evidence_bundle_from_dict` and this array-typed builder refuse the container.
            let iterated = (want == "peer state shape is invalid"
                && e.0 == "peer_states must be an array"
                && !v["peer_states"].is_array())
                || (want == "quarantine shape is invalid"
                    && e.0 == "quarantines must be an array"
                    && !v["quarantines"].is_array());
            if iterated {
                container_artifacts += 1;
            } else if e.0 != want {
                *mismatched
                    .entry(format!("{want:?} <- Rust {:?}", e.0))
                    .or_insert(0usize) += 1;
            }
        }
    }
    assert!(mismatched.is_empty(), "{mismatched:#?}");
    assert_eq!(
        (refusals, compared, container_artifacts),
        (23_604, 23_117, 30)
    );
}
#[test]
fn link_state_validates_full_payload_with_origin_binding() {
    // O:contract:mycelium.link_state.v1.
    corpus(
        include_str!("../fixtures/remaining-gossip-link-vectors.json"),
        |v| gossip::validate_link(v, "node-a").map(|()| v.clone()),
    );
}
#[test]
fn signed_gossip_resigned_adversaries_and_exact_sealing() {
    // O:contract:mycelium.gossip.signed_evidence_bundle.v1.
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../fixtures/remaining-gossip-authority-vectors.json"
    ))
    .unwrap();
    for row in rows {
        let result = gossip::verify_signed(
            &row["value"],
            row["trust"].as_str().unwrap(),
            &row["now"],
            LIMITS,
        );
        assert_eq!(
            result.is_ok(),
            row["accepted"] == true,
            "{}: {row}; {result:?}",
            row["name"]
        );
        if let Ok(v) = result {
            assert_eq!(
                canonical::to_bytes(&v.to_value()).unwrap(),
                row["canonical_utf8"].as_str().unwrap().as_bytes()
            );
        }
    }
    let vector: Value = serde_json::from_str(include_str!(
        "../fixtures/remaining-gossip-seal-vector.json"
    ))
    .unwrap();
    let identity = protocol::identity::Identity::from_secret(
        "test-gossip-authority",
        &std::array::from_fn(|i| i as u8),
    )
    .unwrap();
    let result = gossip::seal_bundle(
        &vector["value"]["evidence_bundle"],
        &identity,
        &vector["captured"],
        &vector["lifetime"],
        &vector["generation"],
        &vector["value"]["capacity_profiles"],
        LIMITS,
    )
    .unwrap();
    assert_eq!(
        canonical::to_bytes(&result).unwrap(),
        vector["canonical_utf8"].as_str().unwrap().as_bytes()
    );
}
#[test]
fn signed_gossip_verifies_independent_authority_time_and_complete_payload() {
    // O:contract:mycelium.gossip.signed_evidence_bundle.v1.
    let signed: Value = serde_json::from_str(include_str!(
        "../fixtures/contracts/signed-gossip-evidence-bundle-v1.json"
    ))
    .unwrap();
    let trust = signed["verification_key"]["verification_key_digest"]
        .as_str()
        .unwrap();
    let now = &signed["statement"]["captured_at_unix_ms"];
    corpus(
        include_str!("../fixtures/remaining-gossip-signed-vectors.json"),
        |v| gossip::verify_signed(v, trust, now, LIMITS).map(|v| v.to_value()),
    );
    let contexts: Vec<Value> = serde_json::from_str(include_str!(
        "../fixtures/remaining-gossip-context-vectors.json"
    ))
    .unwrap();
    for c in contexts {
        let result =
            gossip::verify_signed(&signed, c["trust"].as_str().unwrap(), &c["now"], LIMITS);
        assert_eq!(result.is_ok(), c["accepted"] == true, "{c}; {result:?}");
    }
}
#[test]
fn signed_gossip_refusals_carry_the_oracle_text() {
    // O:contract:mycelium.gossip.signed_evidence_bundle.v1. Every refusal text is the
    // exact `SignedEvidenceBundleError` the pinned v0.1.4 validator raised (recorded
    // from its own execution); v0.1.4 surfaces it on operator CLI stderr, so the bytes
    // are user-visible and are not collapsed to a stable code.
    let signed: Value = serde_json::from_str(include_str!(
        "../fixtures/contracts/signed-gossip-evidence-bundle-v1.json"
    ))
    .unwrap();
    let trust = signed["verification_key"]["verification_key_digest"]
        .as_str()
        .unwrap();
    let now = &signed["statement"]["captured_at_unix_ms"];
    let mut compared = 0usize;
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../fixtures/remaining-gossip-signed-vectors.json"
    ))
    .unwrap();
    for row in &rows {
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            if c["accepted"] == true {
                continue;
            }
            let v = support::mutate(&row["value"], c);
            let err = gossip::verify_signed(&v, trust, now, LIMITS).unwrap_err();
            assert_eq!(err.0, c["error"].as_str().unwrap(), "signed case {i}: {c}");
            compared += 1;
        }
    }
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../fixtures/remaining-gossip-authority-vectors.json"
    ))
    .unwrap();
    for row in &rows {
        if row["accepted"] == true {
            continue;
        }
        let err = gossip::verify_signed(
            &row["value"],
            row["trust"].as_str().unwrap(),
            &row["now"],
            LIMITS,
        )
        .unwrap_err();
        assert_eq!(err.0, row["error"].as_str().unwrap(), "{}", row["name"]);
        compared += 1;
    }
    let contexts: Vec<Value> = serde_json::from_str(include_str!(
        "../fixtures/remaining-gossip-context-vectors.json"
    ))
    .unwrap();
    for c in &contexts {
        if c["accepted"] == true {
            continue;
        }
        let err = gossip::verify_signed(&signed, c["trust"].as_str().unwrap(), &c["now"], LIMITS)
            .unwrap_err();
        assert_eq!(err.0, c["error"].as_str().unwrap(), "{c}");
        compared += 1;
    }
    assert!(compared > 11_000, "{compared}");
}
#[test]
fn signed_gossip_stale_record_text_matches_the_executed_oracle() {
    // Round-6 probe (`round6-signed-gossip-probe.py`): the pinned validator's own
    // execution on freshly sealed test-key bundles; texts the older corpora never hit.
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../fixtures/round6-signed-gossip-extra-vectors.json"
    ))
    .unwrap();
    assert_eq!(rows.len(), 3);
    for row in rows {
        let got = gossip::verify_signed(
            &row["value"],
            row["trust"].as_str().unwrap(),
            &row["now"],
            LIMITS,
        );
        match row["accepted"].as_bool().unwrap() {
            true => assert!(got.is_ok(), "{}", row["name"]),
            false => assert_eq!(
                got.unwrap_err().0,
                row["error"].as_str().unwrap(),
                "{}",
                row["name"]
            ),
        }
    }
}
