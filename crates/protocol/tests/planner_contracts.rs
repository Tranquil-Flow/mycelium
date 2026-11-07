// SPDX-License-Identifier: AGPL-3.0-only
//! `mycelium.control_plane_tranche.v1` and `mycelium.layer_replan_simulation_report.v1`.
//!
//! Both documents are produced/validated by the L7 planner (`planner::bound`,
//! `planner::layer_planner`); `protocol` deliberately holds no copy of that logic. These tests
//! consume the real planner against (a) the two registered oracle fixtures, byte for byte, and
//! (b) the L5-owned round-5 oracle corpora (`round5-*.json`, provenance beside them): systematic
//! mutations with the oracle's actual outputs/exceptions.
use planner::bound::{
    BoundInputs, compile_bound_layer_assignments, validate_control_plane_tranche,
};
use planner::layer_planner::{plan_snapshot, simulate_bundle_file};
use planner::py::{Kind, PyResult};
use protocol::gossip::{self, Limits};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

// Oracle wire hard cap (`mycelium_gossip/schema.py`), not an operational default. TODO(L1).
const LIMITS: Limits = Limits {
    max_record_ttl_ms: 604_800_000,
};
const TRANCHE: &str = include_str!("../fixtures/round5-control-plane-tranche-vectors.json");
const SIMULATION: &str = include_str!("../fixtures/round5-layer-replan-simulation-vectors.json");
const PROVENANCE: &str = include_str!("../fixtures/round5-planner-provenance.json");
const TRANCHE_FIXTURE: &str = include_str!("../fixtures/contracts/control-plane-tranche-v1.json");
const REPORT_FIXTURE: &str =
    include_str!("../fixtures/contracts/layer-replan-simulation-report-v1.json");

fn bytes(v: &Value) -> String {
    planner::canonical(v, false).unwrap()
}

fn apply(base: &Value, patch: &Value) -> Value {
    let mut out = base.clone();
    for op in patch.as_array().unwrap() {
        let path = op["path"].as_array().unwrap();
        if path.is_empty() {
            out = op["value"].clone();
            continue;
        }
        let mut cursor = &mut out;
        for (i, key) in path.iter().enumerate() {
            let last = i + 1 == path.len();
            match key {
                Value::String(k) if last => {
                    let map = cursor.as_object_mut().unwrap();
                    if op.get("delete").is_some() {
                        map.remove(k);
                    } else {
                        map.insert(k.clone(), op["value"].clone());
                    }
                }
                Value::Number(n) if last => {
                    cursor[n.as_u64().unwrap() as usize] = op["value"].clone();
                }
                Value::String(k) => cursor = &mut cursor[k.as_str()],
                Value::Number(n) => cursor = &mut cursor[n.as_u64().unwrap() as usize],
                _ => unreachable!(),
            }
        }
    }
    out
}

fn sha256(raw: &str) -> String {
    format!("{:x}", Sha256::digest(raw.as_bytes()))
}

/// Oracle success → byte-identical canonical JSON. Oracle `ValueError` → the same refusal text.
/// Oracle `ValueError` subclass → a `Kind::Value` refusal (text owned by the subclass's owner and
/// checked there). Any other oracle exception class → a refusal. Returns the divergence, if any.
fn check(label: &str, oracle: &Value, got: PyResult<Value>) -> Option<String> {
    if oracle["ok"] == true {
        return match got {
            Err(e) => Some(format!("{label}: oracle accepted, Rust refused: {e}")),
            Ok(got) if bytes(&oracle["value"]) != bytes(&got) => {
                Some(format!("{label}: accepted with different bytes"))
            }
            Ok(_) => None,
        };
    }
    let Err(err) = got else {
        return Some(format!(
            "{label}: oracle raised {} {:?}; Rust accepted",
            oracle["class"], oracle["message"]
        ));
    };
    if oracle["class"] == "ValueError" {
        if err.kind != Kind::Value || oracle["message"].as_str().unwrap() != err.message {
            return Some(format!(
                "{label}: oracle ValueError {:?}; Rust {:?} {:?}",
                oracle["message"], err.kind, err.message
            ));
        }
    } else if oracle["is_value_error"] == true && err.kind != Kind::Value {
        return Some(format!(
            "{label}: oracle {} ; Rust {:?}",
            oracle["class"], err.kind
        ));
    }
    None
}

fn corpus_counts(cases: &Value) -> (usize, usize) {
    let all = cases.as_array().unwrap();
    let accepted = all.iter().filter(|c| c["oracle"]["ok"] == true).count();
    (accepted, all.len() - accepted)
}

#[test]
fn provenance_pins_exact_oracle_commit_inputs_outputs_and_counts() {
    // O:contract:mycelium.control_plane_tranche.v1; O:contract:mycelium.layer_replan_simulation_report.v1.
    let p: Value = serde_json::from_str(PROVENANCE).unwrap();
    assert_eq!(
        p["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    assert_eq!(p["python"], "3.14.8");
    assert_eq!(
        p["outputs"]["round5-control-plane-tranche-vectors.json"],
        sha256(TRANCHE)
    );
    assert_eq!(
        p["outputs"]["round5-layer-replan-simulation-vectors.json"],
        sha256(SIMULATION)
    );
    assert_eq!(
        p["inputs"]["control-plane-tranche-v1.json"],
        sha256(TRANCHE_FIXTURE)
    );
    assert_eq!(
        p["inputs"]["layer-replan-simulation-report-v1.json"],
        sha256(REPORT_FIXTURE)
    );
    let tranche: Value = serde_json::from_str(TRANCHE).unwrap();
    let simulation: Value = serde_json::from_str(SIMULATION).unwrap();
    assert_eq!(
        p["counts"]["tranche"],
        tranche["cases"].as_array().unwrap().len()
    );
    assert_eq!(
        p["counts"]["simulation"],
        simulation["cases"].as_array().unwrap().len()
    );
    let (accepted, refused) = corpus_counts(&tranche["cases"]);
    assert_eq!(
        (
            p["tranche"]["accepted"].as_u64(),
            p["tranche"]["refused"].as_u64()
        ),
        (Some(accepted as u64), Some(refused as u64))
    );
    let (accepted, refused) = corpus_counts(&simulation["cases"]);
    assert_eq!(
        (
            p["simulation"]["accepted"].as_u64(),
            p["simulation"]["refused"].as_u64()
        ),
        (Some(accepted as u64), Some(refused as u64))
    );
    // The registered base documents are the corpora's bases, not a re-derived copy.
    assert_eq!(
        bytes(&tranche["base"]),
        bytes(&serde_json::from_str::<Value>(TRANCHE_FIXTURE).unwrap())
    );
}

#[test]
fn tranche_fixture_is_recompiled_byte_exact_by_the_real_bound_compiler() {
    // O:contract:mycelium.control_plane_tranche.v1.
    let tranche: Value = serde_json::from_str(TRANCHE_FIXTURE).unwrap();
    let corpus: Value = serde_json::from_str(TRANCHE).unwrap();
    let manifest = &corpus["manifest"];
    validate_control_plane_tranche(&tranche, manifest, LIMITS).unwrap();
    // Re-derive the compiler inputs exactly as the oracle validator does, then require the
    // compiler output to equal the registered assignments byte for byte.
    let assignments = tranche["assignments"].as_array().unwrap();
    let (mut cache_roots, mut runtime_by_node) = (json!({}), json!({}));
    for a in assignments {
        let node = a["node_id"].as_str().unwrap();
        cache_roots[node] = a["artifact_cache_root"].clone();
        runtime_by_node[node] = json!({
            "backend": a["runtime"]["backend"],
            "dtype": a["runtime"]["dtype"],
            "quantization": a["runtime"]["quantization"],
        });
    }
    let compiled = compile_bound_layer_assignments(&BoundInputs {
        route_plan: &tranche["route_plan"],
        planner_snapshot: &tranche["planner_snapshot"],
        evidence_bundle: &tranche["evidence_bundle"],
        manifest,
        deployment_id: &assignments[0]["deployment_id"],
        deployment_epoch: &assignments[0]["deployment_epoch"],
        cache_roots: &cache_roots,
        runtime_by_node: &runtime_by_node,
        limits: LIMITS,
    })
    .unwrap();
    assert_eq!(
        bytes(&tranche["assignments"]),
        bytes(&Value::Array(compiled))
    );
    // The route plan is the real snapshot planner's output for the bound snapshot, and the bundle
    // is exactly what the protocol evidence parser re-derives.
    let plan = plan_snapshot(&tranche["planner_snapshot"])
        .unwrap()
        .to_value()
        .unwrap();
    assert_eq!(bytes(&tranche["route_plan"]), bytes(&plan));
    assert_eq!(
        bytes(&tranche["evidence_bundle"]),
        bytes(&gossip::parse_bundle(&tranche["evidence_bundle"], LIMITS).unwrap())
    );
}

#[test]
fn tranche_oracle_corpus_positive_and_every_refusal_match() {
    // O:contract:mycelium.control_plane_tranche.v1: 4,092 oracle outcomes over systematic
    // delete/null/type/boundary/reorder/duplicate mutations of every node of the registered
    // document and of the manifest.
    let corpus: Value = serde_json::from_str(TRANCHE).unwrap();
    let (base, base_manifest) = (&corpus["base"], &corpus["manifest"]);
    let (mut accepted, mut refused, mut evidence_texts) = (0, 0, 0);
    let mut failures = Vec::new();
    for case in corpus["cases"].as_array().unwrap() {
        let label = case["label"].as_str().unwrap();
        let document = apply(base, &case["patch"]);
        let manifest = apply(base_manifest, &case["manifest_patch"]);
        let got =
            validate_control_plane_tranche(&document, &manifest, LIMITS).map(|()| Value::Null);
        let mut oracle = case["oracle"].clone();
        if oracle["ok"] == true {
            accepted += 1;
            oracle["value"] = Value::Null;
        } else {
            refused += 1;
        }
        failures.extend(check(label, &oracle, got));
        // Evidence refusals surface the oracle's per-field text from the protocol parser (the
        // planner currently collapses it to one message; that is L7's seam, the text is ours).
        if oracle["class"] == "EvidenceBundleError" {
            evidence_texts += 1;
            let text = gossip::parse_bundle(&document["evidence_bundle"], LIMITS)
                .map(|_| String::new())
                .unwrap_or_else(|e| e.0.to_owned());
            if text != oracle["message"].as_str().unwrap() {
                failures.push(format!(
                    "{label}: evidence text {text:?} != {}",
                    oracle["message"]
                ));
            }
        }
    }
    assert!(
        failures.is_empty(),
        "{} divergences, first: {:#?}",
        failures.len(),
        &failures[..failures.len().min(25)]
    );
    assert_eq!((accepted, refused), (740, 3_352));
    assert_eq!(evidence_texts, 780);
}

/// Runs the real simulator on a scratch scenario directory laid out like the oracle's
/// (`root/scn/bundle.json` plus the `files` map, relative to `scn`; `..` reaches `root`).
fn simulate(bundle: &Value, files: &Value, base: &Value, base_name: &str) -> PyResult<Value> {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let root = std::env::temp_dir().join(format!(
        "l5-sim-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    let scn = root.join("scn");
    std::fs::create_dir_all(&scn).unwrap();
    let write = |name: &str, content: &Value| {
        let path = scn.join(name);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        match content.as_str() {
            Some(raw) => std::fs::write(path, raw).unwrap(),
            None => std::fs::write(path, serde_json::to_vec(content).unwrap()).unwrap(),
        }
    };
    if files.is_null() {
        write(base_name, base);
    } else {
        for (name, content) in files.as_object().unwrap() {
            write(name, content);
        }
    }
    write("bundle.json", bundle);
    let result = simulate_bundle_file(&scn.join("bundle.json")).map_err(|mut e| {
        // The oracle corpus normalises its scratch root the same way.
        // Longest spelling first so a symlinked temp root (`/var` → `/private/var`) normalises whole.
        for spelling in [root.canonicalize().unwrap(), root.clone()] {
            e.message = e.message.replace(spelling.to_str().unwrap(), "<root>");
        }
        e
    });
    std::fs::remove_dir_all(&root).unwrap();
    result
}

fn replicated_base() -> Value {
    let corpus: Value = serde_json::from_str(SIMULATION).unwrap();
    corpus["base_snapshot"].clone()
}

#[test]
fn replan_report_fixture_is_reproduced_byte_exact_by_the_real_replanner() {
    // O:contract:mycelium.layer_replan_simulation_report.v1.
    let corpus: Value = serde_json::from_str(SIMULATION).unwrap();
    let report = simulate(
        &corpus["base_bundle"],
        &Value::Null,
        &replicated_base(),
        corpus["base_name"].as_str().unwrap(),
    )
    .unwrap();
    let fixture: Value = serde_json::from_str(REPORT_FIXTURE).unwrap();
    assert_eq!(bytes(&fixture), bytes(&report));
    assert_eq!(
        report["protocol"],
        "mycelium.layer_replan_simulation_report.v1"
    );
    assert_eq!(report["handoff_state"], "placement_intent_only");
    assert_eq!(report["cases"].as_array().unwrap().len(), 4);
    // Planning the same base snapshot directly yields the report's base plan digest.
    let plan = plan_snapshot(&replicated_base()).unwrap();
    assert_eq!(report["base_plan_digest"], plan.digest().unwrap());
}

#[test]
fn replan_simulation_oracle_corpus_positive_and_every_refusal_match() {
    // O:contract:mycelium.layer_replan_simulation_report.v1: 540 oracle outcomes (reports and
    // exceptions) over bundle, path-escape, snapshot, event, threshold and per-case mutations.
    let corpus: Value = serde_json::from_str(SIMULATION).unwrap();
    let (mut accepted, mut refused) = (0, 0);
    let base = replicated_base();
    let mut failures = Vec::new();
    for case in corpus["cases"].as_array().unwrap() {
        let label = case["label"].as_str().unwrap();
        let got = simulate(
            &case["bundle"],
            &case["files"],
            &base,
            corpus["base_name"].as_str().unwrap(),
        );
        if case["oracle"]["ok"] == true {
            accepted += 1;
        } else {
            refused += 1;
        }
        failures.extend(check(label, &case["oracle"], got));
    }
    assert!(
        failures.is_empty(),
        "{} divergences, first: {:#?}",
        failures.len(),
        &failures[..failures.len().min(25)]
    );
    assert_eq!((accepted, refused), (246, 294));
}
