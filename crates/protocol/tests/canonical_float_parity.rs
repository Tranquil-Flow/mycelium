// SPDX-License-Identifier: AGPL-3.0-only
//! `protocol::canonical` float output against CPython 3.14.2 `repr(float)` (== `json.dumps`), with
//! the exact shortest-digit ties dtoa.c mode 0 breaks to the even last digit, and the same tie values
//! through each reachable caller's public path against its v0.1.4 producer's bytes.
//!
//! Fixture `fixtures/canonical-float-ties.json`, written by executing the oracle
//! (`canonical_float_ties_probe.py`; command, CPython version, oracle commit, source hashes and counts
//! in its `provenance` block). The tie rows are every exact tie of the reviewer's seeded sample
//! (`canonical_sample.py`, seed 0x5a17c0de, rv-l1b-m16-coordinator-core).
use protocol::observation::{self, ExpectedObservation};
use protocol::{canonical, capacity, gossip, identity, router_wire, strict};
use serde_json::Value;
use std::collections::BTreeSet;

// Oracle wire hard cap (gossip_contracts.rs), not an operational expiry default.
const LIMITS: gossip::Limits = gossip::Limits {
    max_record_ttl_ms: 604_800_000,
};
const STRICT_MAX_BYTES: usize = 1 << 16;

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/canonical-float-ties.json")).expect("fixture JSON")
}

fn text(v: &Value) -> &str {
    v.as_str().expect("fixture string")
}

fn double(bits: &Value) -> f64 {
    f64::from_bits(u64::from_str_radix(text(bits), 16).expect("fixture bits"))
}

fn utf8(bytes: Vec<u8>) -> String {
    String::from_utf8(bytes).expect("canonical output is UTF-8")
}

#[test]
fn canonical_float_repr_matches_cpython_ties() {
    let v = fixture();
    let counts = &v["provenance"]["counts"];
    let repr = v["repr"].as_object().expect("repr cases");
    assert_eq!(
        repr.keys().map(String::as_str).collect::<Vec<_>>(),
        [
            "decimal_boundary",
            "edges",
            "powers_of_two",
            "thresholds",
            "ties"
        ]
    );
    for (category, count) in [
        ("ties", "ties"),
        ("edges", "edges"),
        ("decimal_boundary", "decimal_boundary"),
        ("thresholds", "thresholds"),
        ("powers_of_two", "powers_of_two"),
    ] {
        assert_eq!(
            repr[category].as_array().map(Vec::len),
            counts[count].as_u64().map(|n| n as usize),
            "{category}"
        );
    }
    assert_eq!(counts["ties"], 8277);
    assert_eq!(counts["divergent_ties"], 4142);
    let names: BTreeSet<&str> = repr["edges"]
        .as_array()
        .expect("edges")
        .iter()
        .map(|r| text(&r[0]))
        .collect();
    for required in [
        "negative_zero",
        "zero",
        "min_subnormal",
        "min_normal",
        "f64_max",
        "1e16",
        "9999999999999998",
        "1e-4",
        "1e-5",
        "1e22",
        "1e23",
        "witness",
        "negative_witness",
        "2^-25",
        "2^-24",
    ] {
        assert!(names.contains(required), "edge {required} missing");
    }

    let mut total = 0usize;
    let mut differ = Vec::new();
    for (category, rows) in repr {
        for r in rows.as_array().expect("rows") {
            let r = r.as_array().expect("row");
            let (bits, expected) = (&r[r.len() - 2], text(&r[r.len() - 1]));
            let x = double(bits);
            total += 1;
            // The shared writer, and the canonical document writer over both a constructed f64 and
            // the CPython literal read back through serde_json (strict ingress keeps the literal).
            let direct = canonical::python_float_repr(x);
            let constructed = serde_json::Number::from_f64(x)
                .map(|n| canonical::to_bytes(&Value::Number(n)).map(utf8));
            let literal = serde_json::from_str::<Value>(expected)
                .map(|n| canonical::to_bytes(&n).map(utf8))
                .ok();
            let want = Ok(expected.to_owned());
            if direct != want || constructed != Some(want.clone()) || literal != Some(want) {
                differ.push(format!(
                    "{category} {} want {expected} got {direct:?}/{constructed:?}/{literal:?}",
                    text(bits)
                ));
            }
        }
    }
    assert_eq!(total, 10_604);
    assert!(
        differ.is_empty(),
        "{} of {total} doubles differ from CPython repr; first {:#?}",
        differ.len(),
        &differ[..differ.len().min(6)]
    );
    // Non-finite input is still refused.
    for x in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        assert_eq!(
            canonical::python_float_repr(x),
            Err(canonical::CanonicalError::NonFinite)
        );
    }
}

fn check<T: PartialEq + std::fmt::Debug>(failures: &mut Vec<String>, label: &str, got: T, want: T) {
    if got != want {
        failures.push(format!("{label}: got {got:?}, want {want:?}"));
    }
}

fn noncanonical() -> protocol::ContractError {
    protocol::ContractError("noncanonical")
}

fn secret(case: &Value) -> [u8; 32] {
    hex::decode(text(&case["secret_hex"]))
        .expect("secret")
        .try_into()
        .expect("32 bytes")
}

#[test]
fn canonical_float_repr_reachable_callers_parity() {
    let v = fixture();
    let mut seen = BTreeSet::new();
    let mut failures = Vec::new();
    let f = &mut failures;
    for case in v["callers"].as_array().expect("callers") {
        let caller = text(&case["caller"]);
        seen.insert(caller);
        match caller {
            // router_wire.rs encode_frame: ReservationRequest/KVSuccessorCutover lease_expires_at
            // and ReservationResult expires_at in the ascii header (mycelium_router/wire.py).
            "router_wire.encode_frame" => {
                let kind = text(&case["kind"]);
                let got = router_wire::encode_frame(kind, &case["body"], b"").map(hex::encode);
                check(
                    f,
                    &format!("{caller} {kind}"),
                    got.as_deref(),
                    Ok(text(&case["encoded_hex"])),
                );
            }
            // capacity.rs parse: strict canonical ingress, reconstruction, profile_digest
            // (compile_capacity_profile(...).canonical_json_bytes()).
            "capacity.parse" => {
                let raw = text(&case["raw_utf8"]);
                let got = capacity::parse(raw.as_bytes());
                check(
                    f,
                    caller,
                    got.as_ref().map(|p| p["profile_digest"].clone()),
                    Ok(case["profile_digest"].clone()),
                );
                check(
                    f,
                    caller,
                    got.and_then(|p| canonical::to_bytes(&p).map_err(|_| noncanonical()))
                        .map(utf8),
                    Ok(raw.to_owned()),
                );
            }
            // identity.rs sign/verify: the signed statement digest (Ed25519EvidenceSigner.sign).
            "identity.sign" => {
                let signer =
                    identity::Identity::from_secret(text(&case["endpoint_id"]), &secret(case))
                        .expect("identity");
                check(
                    f,
                    caller,
                    signer.public_key_record(),
                    case["verification_key"].clone(),
                );
                check(
                    f,
                    caller,
                    signer.sign(&case["statement"]),
                    Ok(case["signature"].clone()),
                );
                check(
                    f,
                    caller,
                    identity::verify(
                        &case["statement"],
                        &case["signature"],
                        &case["verification_key"],
                    ),
                    Ok(()),
                );
            }
            // observation.rs verify_detailed: signature over the node's observation, then the
            // sealed signed bytes and observation digest of its cancel receipt.
            "observation.verify_detailed" => {
                let e = &case["expected"];
                let expected = ExpectedObservation {
                    event: text(&e["event"]),
                    node_id: text(&e["node_id"]),
                    host_id: text(&e["host_id"]),
                    process_id: &e["process_id"],
                    run_id: text(&e["run_id"]),
                    deployment_id: text(&e["deployment_id"]),
                    endpoint_id: text(&e["endpoint_id"]),
                    key: &case["response"]["result"]["verification_key"],
                };
                let got = observation::verify_detailed(&case["response"], &expected)
                    .map_err(|r| r.code)
                    .and_then(|o| o.cancel_receipt(&case["subject"]).map_err(|e| e.0))
                    .map(|r| {
                        (
                            utf8(r.signed_observation_bytes().to_vec()),
                            r.observation_digest().to_owned(),
                        )
                    });
                let want = (
                    text(&case["signed_utf8"]).to_owned(),
                    text(&case["digest"]).to_owned(),
                );
                check(f, caller, got, Ok(want));
            }
            // gossip/mod.rs build_bundle (evidence_bundle_digest) over records whose payload_hash
            // (gossip/schema.rs) covers tie link measurements; parse_bundle re-derives the digest.
            "gossip.build_bundle" => {
                let want = text(&case["bundle_utf8"]);
                let got = gossip::build_bundle(&case["input"], LIMITS)
                    .and_then(|b| canonical::to_bytes(&b).map_err(|_| noncanonical()))
                    .map(utf8);
                check(f, caller, got.as_deref(), Ok(want));
                let bundle: Value = serde_json::from_str(want).expect("bundle");
                let reparsed = gossip::parse_bundle(&bundle, LIMITS)
                    .and_then(|b| canonical::to_bytes(&b).map_err(|_| noncanonical()))
                    .map(utf8);
                check(f, "gossip.parse_bundle", reparsed.as_deref(), Ok(want));
            }
            // gossip/mod.rs seal_bundle (capacity_documents, capacity_profiles_digest, statement
            // signature) and verify_signed (signed_bundle.py).
            "gossip.seal_bundle" => {
                let signer =
                    identity::Identity::from_secret(text(&case["endpoint_id"]), &secret(case))
                        .expect("identity");
                let want = text(&case["signed_utf8"]);
                let got = gossip::seal_bundle(
                    &case["bundle"],
                    &signer,
                    &case["captured"],
                    &case["lifetime"],
                    &case["generation"],
                    &case["capacity_profiles"],
                    LIMITS,
                )
                .and_then(|s| canonical::to_bytes(&s).map_err(|_| noncanonical()))
                .map(utf8);
                check(f, caller, got.as_deref(), Ok(want));
                let signed: Value = serde_json::from_str(want).expect("signed bundle");
                let verified =
                    gossip::verify_signed(&signed, text(&case["trust"]), &case["captured"], LIMITS);
                check(f, "gossip.verify_signed", verified.map(|_| ()), Ok(()));
            }
            // strict.rs canonical_object/canonical_value: accept exactly the CPython bytes
            // (mycelium_seed.http._canonical_json_loads), refuse a half-up or exact-decimal tie.
            "strict.canonical_object" => {
                let raw = text(&case["raw_utf8"]).as_bytes();
                let want = if case["accepted"] == true {
                    Ok(())
                } else {
                    Err(strict::ParseError("frame_noncanonical"))
                };
                let label = format!("{caller} {}", text(&case["label"]));
                let object = strict::canonical_object(raw, STRICT_MAX_BYTES).map(|_| ());
                check(f, &label, object, want.clone());
                let value = strict::canonical_value(raw, STRICT_MAX_BYTES).map(|_| ());
                check(f, &format!("{label} (value)"), value, want);
            }
            // planner py.rs py_repr: runtime_contracts.py `{actual!r}` of a float model flag.
            "planner.py_repr" => {
                let layers = case["expected_layers"].as_i64().expect("layers");
                let got = planner::runtime::normalize_gpt2_model_config(
                    &case["config"],
                    i128::from(layers),
                )
                .map_err(|e| e.message);
                check(f, caller, got, Err(text(&case["message"]).to_owned()));
            }
            other => panic!("unregistered caller {other}"),
        }
    }
    assert_eq!(
        seen,
        BTreeSet::from([
            "capacity.parse",
            "gossip.build_bundle",
            "gossip.seal_bundle",
            "identity.sign",
            "observation.verify_detailed",
            "planner.py_repr",
            "router_wire.encode_frame",
            "strict.canonical_object",
        ])
    );
    assert!(
        failures.is_empty(),
        "{} caller checks differ from the oracle producers:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
