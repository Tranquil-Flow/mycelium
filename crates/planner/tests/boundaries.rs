// SPDX-License-Identifier: AGPL-3.0-only
use planner::Error;
use planner::placement::{LayerProfile, Member, SplitPolicy, split};
use serde_json::{Value, json};
#[test]
fn all_small_contiguous_partitions_match_independent_feasibility() {
    let profile = LayerProfile {
        model_type: "qwen2",
        layer_bytes: vec![1, 3, 2, 5, 1, 4],
        embedding_bytes: 3,
        output_bytes: 2,
        tie_word_embeddings: true,
    };
    let members:Vec<Member>=serde_json::from_value(json!([{"node_id":"n0","memory_bytes":100},{"node_id":"n1","memory_bytes":200},{"node_id":"n2","memory_bytes":300}])).unwrap();
    let policy:SplitPolicy=serde_json::from_value(json!({"memory_fraction_numerator":1,"memory_fraction_denominator":2,"resident_factor_numerator":1,"resident_factor_denominator":1,"runtime_overhead_bytes":0,"speed_clamp_low_numerator":1,"speed_clamp_low_denominator":2,"speed_clamp_high":2})).unwrap();
    for a in [5, 9, 14, 25] {
        for b in [5, 9, 14, 25] {
            for c in [5, 9, 14, 25] {
                let caps = [a, b, c];
                let mut legal = Vec::new();
                for x in 1..6 {
                    for y in x + 1..6 {
                        let sizes = [
                            3 + profile.layer_bytes[..x].iter().sum::<u64>(),
                            profile.layer_bytes[x..y].iter().sum(),
                            5 + profile.layer_bytes[y..].iter().sum::<u64>(),
                        ];
                        if sizes.iter().zip(caps).all(|(need, cap)| *need <= cap) {
                            legal.push(vec![x, y, 6]);
                        }
                    }
                }
                let result = split(&members, &profile, &policy, |s, m| {
                    let index = members.iter().position(|v| v.node_id == m.node_id).unwrap();
                    if s.stage_bytes > caps[index] {
                        Err(Error::node("member_storage_cap_below_share", &m.node_id))
                    } else {
                        Ok(())
                    }
                });
                if legal.is_empty() {
                    assert_eq!(result.unwrap_err().code, "member_storage_cap_below_share");
                } else {
                    let doc = result.unwrap();
                    let ends: Vec<usize> = doc["stages"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|s| s["end"].as_u64().unwrap() as usize)
                        .collect();
                    assert!(legal.contains(&ends));
                }
            }
        }
    }
}
#[test]
fn numeric_overflow_and_invalid_input_refuse_without_panics() {
    let f: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    let case = &f["cases"][0];
    let model = &f["models"][0];
    let p = &f["policies"]["int8-weight-only"];
    let mut bad = model.clone();
    bad["shape"]["hidden_size"] = json!(u64::MAX);
    let shape: planner::placement::ModelShape =
        serde_json::from_value(bad["shape"].clone()).unwrap();
    assert_eq!(
        shape.profile().unwrap_err().code,
        "planner_arithmetic_overflow"
    );
    assert_eq!(
        planner::plan(&bad, p, &f["resources"], case)
            .unwrap_err()
            .code,
        "model_registry_mismatch"
    );
    let mut bad = case.clone();
    bad["members"][0]["node_id"] = json!("../foreign");
    assert_eq!(
        planner::plan(model, p, &f["resources"], &bad)
            .unwrap_err()
            .code,
        "member_capability_invalid"
    );
    let mut bad = case.clone();
    bad["members"][1]["node_id"] = bad["members"][0]["node_id"].clone();
    assert_eq!(
        planner::plan(model, p, &f["resources"], &bad)
            .unwrap_err()
            .code,
        "member_duplicate"
    );
}
