// SPDX-License-Identifier: AGPL-3.0-only
//! CL-L7-02 / CL-L7-03: every stage against RAM ceiling, standing storage/RAM caps and free disk
//! (staging copy, KV, load peak and tied-embedding/head accounting), early and specific refusals.
//! Vectors are the pinned oracle's `plan_for_members` outcomes over a seeded random sweep, 210
//! bisected boundary flips (RAM, storage cap, RAM cap, free disk; flip-2..flip+2) and malformed
//! capability values (`fixtures/stage-sweep.json`, generator and provenance beside it,
//! FX-L7-stage-sweep-r10). An ok case must reproduce the canonical sha256 of `{split, members}`;
//! a refusal must reproduce code and node id; an oracle exception must be a Rust refusal.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

fn outcome(base: &Value, c: &Value) -> Value {
    let model = base["models"]
        .as_array()
        .unwrap()
        .iter()
        .find(|m| m["model_id"] == c["model_id"] && m["resolved_commit"] == c["resolved_commit"])
        .expect("registered model in planning.json");
    let request = json!({
        "model_id": c["model_id"], "members": c["members"], "quantization": c["quantization"],
        "slice_mode": c["slice_mode"], "owner_node_id": c["owner_node_id"],
    });
    let policy = &base["policies"][c["quantization"].as_str().unwrap()];
    match planner::plan(model, policy, &base["resources"], &request) {
        Ok(v) => {
            let body = json!({"split": v["split"], "members": v["members"]});
            let digest = format!(
                "{:x}",
                Sha256::digest(planner::canonical(&body, true).unwrap().as_bytes())
            );
            json!({"ok": true, "sha256": digest})
        }
        Err(e) => json!({"refusal": {"code": e.code, "node_id": e.node_id}}),
    }
}

/// Python ints beyond u64 cannot be a JSON number in `serde_json::Value` (V03-L7-8: sizes are u64; no
/// real memory size exceeds it). The oracle plans a 10**30-byte Mac; Rust refuses the capability.
/// Each row must be observed, any other mismatch fails.
const UNREPRESENTABLE: [&str; 2] = [
    "odd-0-memory_bytes=1000000000000000000000000000000",
    "odd-1-memory_bytes=1000000000000000000000000000000",
];

fn matches(oracle: &Value, rust: &Value) -> bool {
    if oracle.get("raised").is_some() {
        return rust.get("refusal").is_some();
    }
    oracle == rust
}

#[test]
fn oracle_stage_sweep_verdicts_are_reproduced() {
    let base: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    let sweep: Value = serde_json::from_str(include_str!("fixtures/stage-sweep.json")).unwrap();
    let cases = sweep["cases"].as_array().unwrap();
    assert!(cases.len() >= 4000, "corpus shrank: {}", cases.len());
    let mut tally: BTreeMap<String, usize> = BTreeMap::new();
    let mut mismatches = Vec::new();
    let mut pinned = Vec::new();
    for c in cases {
        let rust = outcome(&base, c);
        let key = if c["oracle"].get("ok").is_some() {
            "ok".to_owned()
        } else if let Some(class) = c["oracle"].get("raised") {
            format!("raised:{class}")
        } else {
            format!("refusal:{}", c["oracle"]["refusal"]["code"])
        };
        *tally.entry(key).or_default() += 1;
        if !matches(&c["oracle"], &rust) && UNREPRESENTABLE.contains(&c["label"].as_str().unwrap())
        {
            pinned.push(c["label"].as_str().unwrap().to_owned());
        } else if !matches(&c["oracle"], &rust) {
            mismatches.push(format!(
                "{}: oracle {} rust {}",
                c["label"], c["oracle"], rust
            ));
        }
    }
    for code in [
        "ok",
        "refusal:\"member_cannot_hold_share\"",
        "refusal:\"member_storage_cap_below_share\"",
        "refusal:\"member_memory_cap_below_share\"",
        "refusal:\"member_disk_insufficient_for_share\"",
        "refusal:\"member_memory_insufficient_for_share\"",
    ] {
        assert!(
            tally.get(code).copied().unwrap_or(0) >= 200,
            "{code}: {tally:?}"
        );
    }
    assert_eq!(
        pinned, UNREPRESENTABLE,
        "every pinned divergence must still be observed"
    );
    assert!(
        mismatches.is_empty(),
        "{} of {} cases differ from the oracle ({tally:?}); first 30:\n{}",
        mismatches.len(),
        cases.len(),
        mismatches
            .iter()
            .take(30)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}
