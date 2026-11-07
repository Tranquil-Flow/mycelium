// SPDX-License-Identifier: AGPL-3.0-only
//! Round 6: the v0.1.4 bound compiler support against actual oracle outputs/exceptions
//! (`fixtures/bound-compiler.json`, provenance in `bound-compiler-provenance.json`).
use planner::bound::{
    BoundInputs, compile_bound_layer_assignments, compile_layer_assignments,
    validate_control_plane_tranche,
};
use planner::py::{Kind, PyResult};
use planner::runtime;
use protocol::gossip::Limits;
use serde_json::Value;

// Seven-day source hard cap of the gossip record TTL (mycelium_gossip/schema.py:452).
const LIMITS: Limits = Limits {
    max_record_ttl_ms: 604_800_000,
};

fn fixtures() -> Value {
    serde_json::from_str(include_str!("fixtures/bound-compiler.json")).unwrap()
}

fn provenance() -> Value {
    serde_json::from_str(include_str!("fixtures/bound-compiler-provenance.json")).unwrap()
}

fn apply(base: &Value, patch: &Value) -> Value {
    let mut out = base.clone();
    for op in patch.as_array().unwrap() {
        let path = op["path"].as_array().unwrap();
        let mut cursor = &mut out;
        for (i, key) in path.iter().enumerate() {
            let last = i + 1 == path.len();
            let slot = match key {
                Value::String(k) => {
                    if last {
                        if op.get("delete").is_some() {
                            cursor.as_object_mut().unwrap().remove(k);
                            break;
                        }
                        cursor
                            .as_object_mut()
                            .unwrap()
                            .insert(k.clone(), op["value"].clone());
                        break;
                    }
                    &mut cursor[k.as_str()]
                }
                Value::Number(n) => {
                    let index = n.as_u64().unwrap() as usize;
                    if last {
                        cursor[index] = op["value"].clone();
                        break;
                    }
                    &mut cursor[index]
                }
                _ => unreachable!(),
            };
            cursor = slot;
        }
        if path.is_empty() {
            out = op["value"].clone();
        }
    }
    out
}

fn bytes(v: &Value) -> String {
    planner::canonical(v, false).unwrap()
}

/// Oracle success → byte-identical value. Oracle `ValueError` → same refusal text.
/// Oracle ValueError subclasses (evidence/schema owned by the shared L5 boundary) →
/// a refusal, text owned by L5. Any other oracle exception class → a refusal.
fn check(label: &str, oracle: &Value, got: PyResult<Value>) {
    if oracle["ok"] == true {
        let got = got.unwrap_or_else(|e| panic!("{label}: oracle accepted, Rust refused: {e}"));
        assert_eq!(bytes(&oracle["value"]), bytes(&got), "{label}");
        return;
    }
    let err = match got {
        Ok(v) => panic!(
            "{label}: oracle raised {} {:?}; Rust accepted {}",
            oracle["class"],
            oracle["message"],
            bytes(&v)
        ),
        Err(e) => e,
    };
    // `EvidenceBundleError` (a ValueError subclass raised by the gossip evidence contract) carries
    // a per-field text that v0.1.4 callers observe; L5 returns it exactly (99cd3f7).
    if oracle["class"] == "ValueError" || oracle["class"] == "EvidenceBundleError" {
        assert_eq!(err.kind, Kind::Value, "{label}: {err}");
        assert_eq!(oracle["message"].as_str().unwrap(), err.message, "{label}");
    } else if oracle["is_value_error"] == true {
        assert_eq!(err.kind, Kind::Value, "{label}: {err}");
    }
}

#[test]
fn provenance_pins_the_exact_fixture_and_counts() {
    use sha2::{Digest, Sha256};
    let p = provenance();
    let raw = include_str!("fixtures/bound-compiler.json");
    assert_eq!(
        p["output_sha256"].as_str().unwrap(),
        format!("{:x}", Sha256::digest(raw.as_bytes()))
    );
    assert_eq!(
        p["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let f = fixtures();
    assert_eq!(
        p["counts"]["runtime"],
        f["runtime"].as_array().unwrap().len()
    );
    assert_eq!(
        p["counts"]["normalizers"],
        f["normalizers"].as_array().unwrap().len()
    );
    assert_eq!(
        p["counts"]["layer_compile"],
        f["layer_compile"].as_array().unwrap().len()
    );
    assert_eq!(
        p["counts"]["bound"]["bound"],
        f["bound"]["bound"].as_array().unwrap().len()
    );
    assert_eq!(
        p["counts"]["bound"]["tranche"],
        f["bound"]["tranche"].as_array().unwrap().len()
    );
}

#[test]
fn normalized_runtime_contracts_match_the_oracle_for_every_architecture_and_backend() {
    let f = fixtures();
    let (mut ok, mut refused) = (0, 0);
    for case in f["runtime"].as_array().unwrap() {
        let backend = case["expected_backend"].as_str();
        let got = runtime::validate_normalized_runtime(&case["input"], backend);
        if case["oracle"]["ok"] == true {
            ok += 1;
        } else {
            refused += 1;
        }
        check(&format!("runtime {}", case["input"]), &case["oracle"], got);
    }
    // GPT-2 support is real: an accepted GPT-2 identity exists for mlx float16 and numpy float32.
    assert!(
        f["runtime"].as_array().unwrap().iter().any(|c| {
            c["oracle"]["ok"] == true && c["oracle"]["value"]["architecture"] == "gpt2"
        })
    );
    assert!(
        ok >= 100 && refused >= 600,
        "{ok} accepted / {refused} refused"
    );
}

#[test]
fn model_config_normalizers_match_the_oracle_including_hybrid_and_moe_sources() {
    let f = fixtures();
    for case in f["normalizers"].as_array().unwrap() {
        let layers = i128::from(case["expected_layers"].as_i64().unwrap());
        let config = &case["config"];
        let got = match case["fn"].as_str().unwrap() {
            "normalize_gpt2" => runtime::normalize_gpt2_model_config(config, layers),
            "normalize_qwen2" => runtime::normalize_qwen2_model_config(config, layers),
            "normalize_qwen3" => runtime::normalize_qwen3_model_config(config, layers),
            "normalize_qwen3_5" => runtime::normalize_qwen35_model_config(config, layers),
            "normalize_qwen3_5_moe" => runtime::normalize_qwen35_moe_model_config(config, layers),
            other => panic!("{other}"),
        };
        check(&format!("{} {config}", case["fn"]), &case["oracle"], got);
    }
}

#[test]
fn layer_assignment_compiler_matches_the_oracle_for_gpt2_and_qwen2_manifests() {
    let f = fixtures();
    let (mut ok, mut refused) = (0, 0);
    for case in f["layer_compile"].as_array().unwrap() {
        let args = apply(
            &f["layer_bases"][case["base"].as_str().unwrap()],
            &case["patch"],
        );
        let got = compile_layer_assignments(
            &args["route_plan"],
            &args["manifest"],
            &args["deployment_id"],
            &args["deployment_epoch"],
            &args["cache_roots"],
            &args["runtime_by_node"],
            args.get("control_plane_binding"),
        )
        .map(Value::Array);
        if case["oracle"]["ok"] == true {
            ok += 1;
            let runtime = &case["oracle"]["value"][0]["runtime"];
            if case["base"] == "gpt2" && runtime["backend"] != "cuda" {
                assert_eq!(runtime["architecture"], "gpt2", "{}", case["label"]);
            }
        } else {
            refused += 1;
        }
        check(case["label"].as_str().unwrap(), &case["oracle"], got);
    }
    assert!(
        ok >= 150 && refused >= 1000,
        "{ok} accepted / {refused} refused"
    );
}

#[test]
fn bound_compiler_with_lineage_matches_the_oracle() {
    let f = fixtures();
    let base = &f["bound"]["bases"]["bound"];
    let (mut ok, mut refused) = (0, 0);
    for case in f["bound"]["bound"].as_array().unwrap() {
        let a = apply(base, &case["patch"]);
        let got = compile_bound_layer_assignments(&BoundInputs {
            route_plan: &a["route_plan"],
            planner_snapshot: &a["planner_snapshot"],
            evidence_bundle: &a["evidence_bundle"],
            manifest: &a["manifest"],
            deployment_id: &a["deployment_id"],
            deployment_epoch: &a["deployment_epoch"],
            cache_roots: &a["cache_roots"],
            runtime_by_node: &a["runtime_by_node"],
            limits: LIMITS,
        })
        .map(Value::Array);
        if case["oracle"]["ok"] == true {
            ok += 1;
        } else {
            refused += 1;
        }
        check(case["label"].as_str().unwrap(), &case["oracle"], got);
    }
    assert!(
        ok >= 40 && refused >= 600,
        "{ok} accepted / {refused} refused"
    );
}

#[test]
fn control_plane_tranche_validation_matches_the_oracle() {
    let f = fixtures();
    let base = &f["bound"]["bases"]["tranche"];
    let base_manifest = &f["manifests"]["gpt2"];
    let (mut ok, mut refused) = (0, 0);
    for case in f["bound"]["tranche"].as_array().unwrap() {
        let document = apply(base, &case["patch"]);
        let manifest = apply(base_manifest, &case["manifest_patch"]);
        let got =
            validate_control_plane_tranche(&document, &manifest, LIMITS).map(|()| Value::Null);
        if case["oracle"]["ok"] == true {
            ok += 1;
        } else {
            refused += 1;
        }
        let mut oracle = case["oracle"].clone();
        if oracle["ok"] == true {
            oracle["value"] = Value::Null;
        }
        check(case["label"].as_str().unwrap(), &oracle, got);
    }
    assert!(
        ok >= 1 && refused >= 150,
        "{ok} accepted / {refused} refused"
    );
}

/// Round 7 (parent seam item 1): null / numeric / bool / container entries in
/// `legal_tracks[0].placement_ids` — hashable ones refuse with the oracle's ValueError text,
/// unhashable ones with a non-ValueError exception (`fixtures/placement-ids.json`).
#[test]
fn non_string_placement_ids_refuse_like_the_oracle() {
    use sha2::{Digest, Sha256};
    let raw = include_str!("fixtures/placement-ids.json");
    let p: Value =
        serde_json::from_str(include_str!("fixtures/placement-ids-provenance.json")).unwrap();
    assert_eq!(
        p["output_sha256"].as_str().unwrap(),
        format!("{:x}", Sha256::digest(raw.as_bytes()))
    );
    let f: Value = serde_json::from_str(raw).unwrap();
    let (mut value_errors, mut other) = (0, 0);
    for case in f["cases"].as_array().unwrap() {
        let mut a = f["base"].clone();
        a["route_plan"]["legal_tracks"][0]["placement_ids"] = case["placement_ids"].clone();
        let got = compile_bound_layer_assignments(&BoundInputs {
            route_plan: &a["route_plan"],
            planner_snapshot: &a["planner_snapshot"],
            evidence_bundle: &a["evidence_bundle"],
            manifest: &a["manifest"],
            deployment_id: &a["deployment_id"],
            deployment_epoch: &a["deployment_epoch"],
            cache_roots: &a["cache_roots"],
            runtime_by_node: &a["runtime_by_node"],
            limits: LIMITS,
        })
        .map(Value::Array);
        match case["oracle"]["class"].as_str() {
            Some("ValueError") => value_errors += 1,
            Some(_) => other += 1,
            None => {}
        }
        check(case["label"].as_str().unwrap(), &case["oracle"], got);
    }
    assert!(
        value_errors >= 20 && other >= 10,
        "{value_errors} / {other}"
    );
}
