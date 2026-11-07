// SPDX-License-Identifier: AGPL-3.0-only
//! Round 7: `planner::compile::compile_assignments` accepts exactly what v0.1.4's
//! `layer_assignment.compile_layer_assignments` accepts and refuses exactly what it refuses
//! (V02-PLAN 1.1; integrator ruling b). Vectors: `fixtures/legacy-compile.json` (actual oracle
//! outputs/exceptions for UUID spellings, epoch kinds, cache-root spellings, legacy route plans,
//! GPT-2 and every runtime, binding forms and generic mutations of the two recorded inputs) and the
//! 1,406 round-6 layer-compile vectors in `fixtures/bound-compiler.json`.
use planner::compile::compile_assignments;
use serde_json::{Map, Value, json};

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

fn legacy() -> Value {
    serde_json::from_str(include_str!("fixtures/legacy-compile.json")).unwrap()
}

/// Oracle success → byte-identical assignments. Oracle exception → a refusal; for a `ValueError`
/// the Rust error carries the oracle's exact message.
fn check(label: &str, oracle: &Value, got: planner::Result<Value>) {
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
            bytes(&v).chars().take(160).collect::<String>()
        ),
        Err(e) => e,
    };
    if oracle["class"] == "ValueError" {
        assert_eq!(
            err.detail.as_deref(),
            oracle["message"].as_str(),
            "{label}: Rust refused with {err}"
        );
    }
}

#[test]
fn provenance_pins_the_exact_fixture() {
    use sha2::{Digest, Sha256};
    let raw = include_str!("fixtures/legacy-compile.json");
    let p: Value =
        serde_json::from_str(include_str!("fixtures/legacy-compile-provenance.json")).unwrap();
    assert_eq!(
        p["output_sha256"].as_str().unwrap(),
        format!("{:x}", Sha256::digest(raw.as_bytes()))
    );
    assert_eq!(
        p["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let f = legacy();
    assert_eq!(p["counts"]["cases"], f["cases"].as_array().unwrap().len());
}

/// Oracle-accepted inputs that Rust deliberately cannot carry: Python integers beyond the u64
/// range have no `serde_json::Value` representation (no `arbitrary_precision`), so Rust refuses
/// them (fail closed) instead of accepting a different number.
const UNREPRESENTABLE: &[&str] = &[
    "base0:epoch=18446744073709551616",
    "base1:epoch=18446744073709551616",
];

#[test]
fn compile_assignments_matches_the_oracle_on_legacy_inputs_and_mutations() {
    let f = legacy();
    let (mut ok, mut refused) = (0, 0);
    let mut seen_unrepresentable = 0;
    for case in f["cases"].as_array().unwrap() {
        let label = case["label"].as_str().unwrap();
        let input = apply(&f["bases"][case["base"].as_str().unwrap()], &case["patch"]);
        if UNREPRESENTABLE.contains(&label) {
            assert_eq!(case["oracle"]["ok"], true, "{label}");
            assert!(compile_assignments(&input).is_err(), "{label}");
            seen_unrepresentable += 1;
            continue;
        }
        if case["oracle"]["ok"] == true {
            ok += 1;
        } else {
            refused += 1;
        }
        check(label, &case["oracle"], compile_assignments(&input));
    }
    assert_eq!(seen_unrepresentable, UNREPRESENTABLE.len());
    assert!(
        ok >= 100 && refused >= 1000,
        "{ok} accepted / {refused} refused"
    );
}

/// The round-6 layer-compile vectors (GPT-2 and Qwen2 manifests, every runtime backend, lineage
/// binding) through the legacy entry point.
#[test]
fn compile_assignments_matches_the_oracle_on_every_round6_layer_compile_vector() {
    let f: Value = serde_json::from_str(include_str!("fixtures/bound-compiler.json")).unwrap();
    let mut accepted_gpt2 = 0;
    let (mut ok, mut refused) = (0, 0);
    for case in f["layer_compile"].as_array().unwrap() {
        let base = &f["layer_bases"][case["base"].as_str().unwrap()];
        let a = apply(base, &case["patch"]);
        let mut input = Map::new();
        for key in [
            "route_plan",
            "manifest",
            "deployment_id",
            "deployment_epoch",
            "cache_roots",
            "runtime_by_node",
        ] {
            if let Some(v) = a.get(key) {
                input.insert(key.to_owned(), v.clone());
            }
        }
        if let Some(b) = a.get("control_plane_binding") {
            input.insert("control_plane_binding".into(), b.clone());
        }
        let oracle = &case["oracle"];
        if oracle["ok"] == true {
            ok += 1;
            if input["manifest"]["runtime_model"]["architecture"] == json!("gpt2") {
                accepted_gpt2 += 1;
            }
        } else {
            refused += 1;
        }
        check(
            case["label"].as_str().unwrap(),
            oracle,
            compile_assignments(&Value::Object(input)),
        );
    }
    assert!(
        ok >= 100 && refused >= 500,
        "{ok} accepted / {refused} refused"
    );
    assert!(
        accepted_gpt2 >= 10,
        "no accepted GPT-2 vector: {accepted_gpt2}"
    );
}
