// SPDX-License-Identifier: AGPL-3.0-only
//! Resident stage-split resource model for Qwen3.6-35B-A3B, bound to the resident array sizes
//! the pinned oracle actually recorded (decoder + head fixtures). No native library, no weights.
use model::moe_decoder::{
    EMBED_KEY, HEAD_KEY, LAYERS, NORM_KEY, plan_stage_split, resident_bytes, stage_resident_bytes,
    stage_weights,
};
use serde_json::Value;
use stage_contract::ErrorCode;
use std::path::PathBuf;

const GIB: u64 = 1024 * 1024 * 1024;

fn fixture(name: &str) -> Value {
    serde_json::from_slice(
        &std::fs::read(
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(format!("tests/fixtures/{name}.json")),
        )
        .unwrap(),
    )
    .unwrap()
}
fn recorded(entry: &Value) -> u64 {
    entry["resident"]
        .as_object()
        .unwrap()
        .values()
        .map(|a| a["payload_bytes"].as_u64().unwrap())
        .sum()
}

#[test]
fn resident_byte_model_equals_every_oracle_recorded_resident_image() {
    for profile in ["f32", "packed"] {
        let packed = profile == "packed";
        let mut total = 0;
        let mut count = 0;
        for name in ["moe-decoder", "moe-head"] {
            let f = fixture(&format!("{name}-{profile}"));
            for (key, entry) in f["weights"].as_object().unwrap() {
                let shape: Vec<i32> = serde_json::from_value(entry["shape"].clone()).unwrap();
                assert_eq!(
                    resident_bytes(key, &shape, packed).unwrap(),
                    recorded(entry),
                    "{profile} {key}"
                );
                total += recorded(entry);
                count += 1;
            }
        }
        assert_eq!(count, 30 * 18 + 10 * 15 + 3);
        assert_eq!(
            stage_resident_bytes(0..LAYERS, true, true, packed).unwrap(),
            total,
            "{profile} whole-model resident bytes"
        );
    }
}

#[test]
fn two_stage_halves_are_far_over_the_worker_caps_so_the_split_needs_many_stages() {
    // Bytes pinned from the oracle-recorded images above; these are facts about the model, not
    // limits chosen to pass. Worker caps (L1 registry / lane guard): 6 GiB native allocator.
    let cap = 6 * GIB;
    for packed in [true, false] {
        let whole = stage_resident_bytes(0..LAYERS, true, true, packed).unwrap();
        let halves = [
            stage_resident_bytes(0..20, true, false, packed).unwrap(),
            stage_resident_bytes(20..40, false, true, packed).unwrap(),
        ];
        assert_eq!(halves.iter().sum::<u64>(), whole);
        println!(
            "STAGE_RESOURCES packed={packed} whole_bytes={whole} half_bytes={halves:?} stages_at_6GiB={}",
            plan_stage_split(cap, packed).unwrap().len()
        );
        assert!(halves.iter().all(|h| *h > cap), "{halves:?} fits {cap}?");
    }
    let plan = plan_stage_split(cap, true).unwrap();
    assert!(plan.len() >= 6, "packed int8 needs >= 6 stages at 6 GiB");
    assert!(plan[0].embedding && !plan[0].head);
    assert!(plan.last().unwrap().head && !plan.last().unwrap().embedding);
    assert!(plan[1..].iter().all(|s| !s.embedding));
    assert!(plan[..plan.len() - 1].iter().all(|s| !s.head));
    let mut next = 0;
    for stage in &plan {
        assert_eq!(
            stage.layers.start, next,
            "contiguous, ordered, no gap/overlap"
        );
        next = stage.layers.end;
        assert!(stage.resident_bytes <= cap);
        assert_eq!(
            stage.resident_bytes,
            stage_resident_bytes(stage.layers.clone(), stage.embedding, stage.head, true).unwrap()
        );
    }
    assert_eq!(next, LAYERS);
    // Fewer stages are not possible: merging any adjacent pair exceeds the cap.
    for pair in plan.windows(2) {
        let merged = stage_resident_bytes(
            pair[0].layers.start..pair[1].layers.end,
            pair[0].embedding,
            pair[1].head,
            true,
        )
        .unwrap();
        assert!(merged > cap);
    }
    // Float profile at the same cap needs strictly more stages.
    assert!(plan_stage_split(cap, false).unwrap().len() > plan.len());
}

#[test]
fn stage_split_planning_refuses_unplaceable_and_malformed_caps() {
    let layer = stage_resident_bytes(3..4, false, false, true).unwrap();
    assert_eq!(
        plan_stage_split(layer - 1, true).unwrap_err().code,
        ErrorCode::ResourceCap,
        "one layer cannot be split"
    );
    assert_eq!(
        plan_stage_split(0, true).unwrap_err().code,
        ErrorCode::ResourceCap
    );
    // An unbounded cap yields exactly one whole-model stage owning embedding and head.
    let one = plan_stage_split(u64::MAX, true).unwrap();
    assert_eq!(one.len(), 1);
    assert_eq!(
        (one[0].layers.clone(), one[0].embedding, one[0].head),
        (0..LAYERS, true, true)
    );
    let owned = stage_weights(0..LAYERS, true, true).unwrap();
    for key in [EMBED_KEY, NORM_KEY, HEAD_KEY] {
        assert!(owned.contains_key(key));
    }
}
