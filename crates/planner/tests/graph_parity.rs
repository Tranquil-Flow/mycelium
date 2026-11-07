// SPDX-License-Identifier: AGPL-3.0-only
//! Round 9: oracle corpus for graph compilation + load-proof binding. Each case is a patch on an
//! oracle-compiled assignment set (`fixtures/graph-corpus.json`, generator and provenance beside
//! it). The oracle's `build_execution_graph` outcome and its own `_validate_load_proof` verdict
//! per stage are the vectors. Rust must (a) produce byte-identical graph metadata wherever the
//! oracle builds a graph from proofs its validator accepts, (b) refuse wherever the oracle refuses.
//! Rust may additionally refuse inputs the oracle's graph builder alone lets through only when
//! the oracle's validator rejects the proof/assignment pair (the builder is never called without it).
//!
//! Round-9 findings fixed from this corpus: `bfloat16` activations (2 bytes in v0.1.4, refused
//! here), load generation 0 (v0.1.4 refuses only `< 0`; refused here), and a float16 + bfloat16
//! stage mix (v0.1.4 compares the dtype strings; Rust compared widths and accepted it).
use planner::routes::{LegacyGraphCosts, compile_graph};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

fn corpus() -> Value {
    serde_json::from_str(include_str!("fixtures/graph-corpus.json")).unwrap()
}

fn apply(doc: &mut Value, ops: &Value) {
    for op in ops.as_array().unwrap() {
        let path = op["path"].as_array().unwrap();
        let mut cursor = &mut *doc;
        for (i, key) in path.iter().enumerate() {
            let last = i + 1 == path.len();
            match key {
                Value::String(k) if last => {
                    if op.get("delete").is_some() {
                        cursor.as_object_mut().unwrap().remove(k);
                    } else {
                        cursor
                            .as_object_mut()
                            .unwrap()
                            .insert(k.clone(), op["value"].clone());
                    }
                }
                Value::String(k) => cursor = &mut cursor[k.as_str()],
                Value::Number(n) => {
                    let index = n.as_u64().unwrap() as usize;
                    if last {
                        cursor[index] = op["value"].clone();
                    } else {
                        cursor = &mut cursor[index];
                    }
                }
                _ => unreachable!(),
            }
        }
    }
}

/// Inputs the oracle's graph builder lets through but no v0.1.4 compile emits: hand-built
/// assignment sets. Rust refuses them on purpose (every row listed with the group's reason);
/// the test requires each row to be observed and any other mismatch to fail.
///
/// - `scheme`: the builder only requires a non-empty string; callers pass the literals `iroh`,
///   `local-loopback` and `pipe`. Rust restricts it to `[A-Za-z0-9-]` (it is joined into ids).
/// - `roles`: the builder never checks component roles; the compiler only emits embedding on the
///   first stage and head+final_norm on the last, so a graph that violates it cannot embed/decode.
/// - `identity`: the builder silently takes stage 0's deployment/model/manifest/commit for the whole
///   graph; Rust refuses an assignment set that mixes them.
/// - `node`: empty, untrimmed, delimiter-bearing (`/ : ? #`) or duplicate node ids.
/// - `aliases`: the oracle never compares the proof's resolved aliases with the assignment's.
/// - `config`: stages with differing `model_config` (the oracle reads stage 0 only).
/// - `protocol`: the builder does not check the assignment protocol.
const STRICTER: &[(&str, &str, &str)] = &[
    (
        "roles",
        "components[]='decoder'",
        "execution_graph_inputs_invalid",
    ),
    (
        "roles",
        "components[]=['decoder', 'decoder']",
        "execution_graph_components_invalid",
    ),
    (
        "roles",
        "components[]=['decoder', 'final_norm']",
        "execution_graph_components_invalid",
    ),
    (
        "roles",
        "components[]=['decoder', 'lm_head', 'final_norm']",
        "execution_graph_components_invalid",
    ),
    (
        "roles",
        "components[]=['decoder', 'lm_head']",
        "execution_graph_components_invalid",
    ),
    (
        "roles",
        "components[]=['decoder']",
        "execution_graph_components_invalid",
    ),
    (
        "roles",
        "components[]=['input_embedding', 'decoder']",
        "execution_graph_components_invalid",
    ),
    (
        "roles",
        "components[]=['lm_head']",
        "execution_graph_components_invalid",
    ),
    (
        "roles",
        "components[]=[]",
        "execution_graph_components_invalid",
    ),
    (
        "identity",
        "deployment_epoch[]=other",
        "execution_graph_proof_binding",
    ),
    (
        "identity",
        "deployment_id[]=other",
        "assignment_deployment_id_invalid",
    ),
    ("scheme", "link='A_b'", "invalid_execution_graph_scheme"),
    ("scheme", "link='a b'", "invalid_execution_graph_scheme"),
    ("scheme", "link='a/b'", "invalid_execution_graph_scheme"),
    (
        "scheme",
        "link='iroh://x'",
        "invalid_execution_graph_scheme",
    ),
    ("scheme", "link='x:y'", "invalid_execution_graph_scheme"),
    ("scheme", "link='é'", "invalid_execution_graph_scheme"),
    (
        "identity",
        "manifest_digest[]=other",
        "execution_graph_proof_binding",
    ),
    (
        "identity",
        "model_id[]=other",
        "execution_graph_proof_binding",
    ),
    (
        "config",
        "n_layer[]=+",
        "execution_graph_runtime_dtype_mismatch",
    ),
    ("node", "node_id[]=", "execution_graph_inputs_invalid"),
    ("node", "node_id[]=' a'", "execution_graph_inputs_invalid"),
    ("node", "node_id[]='a#b'", "execution_graph_node_invalid"),
    ("node", "node_id[]='a'", "execution_graph_node_invalid"),
    ("node", "node_id[]='a/b'", "execution_graph_node_invalid"),
    ("node", "node_id[]='a:b'", "execution_graph_node_invalid"),
    ("node", "node_id[]='a?b'", "execution_graph_node_invalid"),
    ("node", "node_id[]='b'", "execution_graph_node_invalid"),
    ("node", "node_id[]='n'", "execution_graph_node_invalid"),
    (
        "aliases",
        "proof[].resolved_component_aliases=deleted",
        "execution_graph_proof_binding",
    ),
    (
        "aliases",
        "proof[].resolved_component_aliases=foreign",
        "execution_graph_proof_binding",
    ),
    (
        "aliases",
        "proof[].resolved_component_aliases=null",
        "execution_graph_proof_binding",
    ),
    (
        "aliases",
        "proof[].resolved_component_aliases=number",
        "execution_graph_proof_binding",
    ),
    (
        "protocol",
        "protocol[]=v",
        "execution_graph_assignment_identity",
    ),
    (
        "identity",
        "resolved_commit[]=other",
        "execution_graph_proof_binding",
    ),
    ("scheme", "runtime='A_b'", "invalid_execution_graph_scheme"),
    ("scheme", "runtime='a b'", "invalid_execution_graph_scheme"),
    ("scheme", "runtime='a/b'", "invalid_execution_graph_scheme"),
    (
        "scheme",
        "runtime='iroh://x'",
        "invalid_execution_graph_scheme",
    ),
    ("scheme", "runtime='x:y'", "invalid_execution_graph_scheme"),
    ("scheme", "runtime='é'", "invalid_execution_graph_scheme"),
];

struct Outcome {
    label: String,
    category: &'static str,
    kind: String,
    code: String,
}

fn kind_of(label: &str) -> String {
    // Group by the mutation, not the base or the stage index.
    let kind = label.split_once(" :: ").map_or(label, |(_, m)| m);
    let kind = kind.split_once(" => ").map_or(kind, |(k, _)| k);
    kind.chars()
        .filter(|c| !c.is_ascii_digit() || kind.starts_with("load_generation"))
        .collect()
}

fn run_case(c: &Value, bases: &[Value], costs: &Value) -> Option<Outcome> {
    let base = &bases[c["base"].as_u64().unwrap() as usize];
    let mut doc = serde_json::json!({"assignments": base["assignments"], "proofs": base["proofs"]});
    apply(&mut doc, &c["ops"]);
    if c["assignment_id_recomputed"] == true {
        for k in 0..doc["assignments"].as_array().unwrap().len() {
            if let Ok(id) = planner::compile::assignment_id(&doc["assignments"][k]) {
                doc["assignments"][k]["assignment_id"] = Value::String(id.clone());
                if let Some(p) = doc["proofs"].get_mut(k)
                    && p.get("assignment_id").is_some()
                {
                    p["assignment_id"] = Value::String(id);
                }
            }
        }
    }
    let got = compile_graph(
        doc["assignments"].as_array().unwrap(),
        doc["proofs"].as_array().unwrap(),
        c["link"].as_str().unwrap(),
        c["runtime"].as_str().unwrap(),
        LegacyGraphCosts {
            prefill_work_units: costs["prefill_work_units"].as_f64().unwrap(),
            decode_work_units: costs["decode_work_units"].as_f64().unwrap(),
            kv_bytes_per_token: costs["kv_bytes_per_token"].as_u64().unwrap(),
        },
    );
    let oracle = &c["oracle"];
    let rejected_downstream = oracle["proof_verdict"]
        .as_array()
        .unwrap()
        .iter()
        .any(|v| !v.is_null())
        || oracle
            .get("router_validation")
            .is_some_and(|v| !v.is_null());
    let label = format!(
        "{} :: {}",
        base["label"].as_str().unwrap(),
        c["label"].as_str().unwrap()
    );
    let code = got
        .as_ref()
        .err()
        .map(|e| e.code.to_owned())
        .unwrap_or_default();
    let category = match (oracle["ok"] == true, got) {
        (true, Ok(graph)) => {
            let canonical = planner::canonical(graph.metadata(), false).unwrap();
            let sha = format!("{:x}", Sha256::digest(canonical.as_bytes()));
            if sha == oracle["graph_sha256"].as_str().unwrap() {
                return None;
            }
            "accepted by both, graph metadata differs"
        }
        (true, Err(_)) if rejected_downstream => return None,
        (true, Err(_)) => "oracle builds a graph from validated proofs; Rust refuses",
        (false, Err(_)) => return None,
        (false, Ok(_)) => "oracle refuses; Rust accepts",
    };
    Some(Outcome {
        kind: kind_of(&label),
        label,
        category,
        code,
    })
}

#[test]
fn graph_compilation_matches_the_oracle_on_the_mutation_corpus() {
    let f = corpus();
    let provenance: Value =
        serde_json::from_str(include_str!("fixtures/graph-corpus-provenance.json")).unwrap();
    assert_eq!(
        provenance["output_sha256"].as_str().unwrap(),
        format!(
            "{:x}",
            Sha256::digest(include_str!("fixtures/graph-corpus.json").as_bytes())
        )
    );
    assert_eq!(
        provenance["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let bases = f["bases"].as_array().unwrap();
    let cases = f["cases"].as_array().unwrap();
    let accepted = cases.iter().filter(|c| c["oracle"]["ok"] == true).count();
    assert_eq!(provenance["counts"]["cases"], cases.len());
    assert_eq!(provenance["counts"]["accepted"], accepted);
    assert!(accepted >= 1300 && cases.len() - accepted >= 350);
    let mut unexpected: BTreeMap<String, usize> = BTreeMap::new();
    let mut stricter: BTreeMap<(String, String), usize> = BTreeMap::new();
    for c in cases {
        let Some(o) = run_case(c, bases, &f["costs"]) else {
            continue;
        };
        let listed = o.category == "oracle builds a graph from validated proofs; Rust refuses"
            && STRICTER
                .iter()
                .any(|(_, k, code)| *k == o.kind && *code == o.code);
        if listed {
            *stricter.entry((o.kind, o.code)).or_default() += 1;
        } else {
            *unexpected
                .entry(format!(
                    "{}: {} => {} (e.g. {})",
                    o.category, o.kind, o.code, o.label
                ))
                .or_default() += 1;
        }
    }
    assert!(
        unexpected.is_empty(),
        "{} unexpected mismatch kinds:\n{}",
        unexpected.len(),
        unexpected
            .iter()
            .map(|(k, n)| format!("{n} x {k}"))
            .collect::<Vec<_>>()
            .join("\n")
    );
    for (_, kind, code) in STRICTER {
        assert!(
            stricter.contains_key(&(kind.to_string(), code.to_string())),
            "listed stricter row no longer observed: {kind} => {code}"
        );
    }
}
