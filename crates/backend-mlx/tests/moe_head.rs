// SPDX-License-Identifier: AGPL-3.0-only
//! Qwen3.6-35B-A3B untied embedding, final norm and lm_head against the pinned v0.1.4 oracle
//! (f4e366e9) recorded on the existing layer-39 decoder outputs. Component evidence only: not
//! resident stage/model admission, 64-token decode or transport.
#![cfg(feature = "metal")]
use backend_mlx::Context;
use model::moe_decoder::{
    EMBED_KEY, HEAD_KEY, HIDDEN, VOCAB, embed, embedding_weights, final_norm, head, head_weights,
    select_greedy,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use stage_contract::ErrorCode;
use std::{
    path::{Path, PathBuf},
    rc::Rc,
    time::Instant,
};

mod common;
use common::{checkpoint_root, load_weights, npy_f32, payload, quiet};

fn head_component(profile: &str) {
    quiet();
    let started = Instant::now();
    let fixture: Value = serde_json::from_slice(
        &std::fs::read(
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join(format!("tests/fixtures/moe-head-{profile}.json")),
        )
        .expect("mandatory recorded MoE head fixture"),
    )
    .unwrap();
    assert_eq!(fixture["schema"], "l8-moe-head-component-v1");
    assert_eq!(
        fixture["scope"],
        "embedding-final-norm-untied-head-not-resident-stage-model-or-transport-admission"
    );
    assert_eq!(
        fixture["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    assert_eq!(fixture["profile"], profile);
    let successor: Value =
        serde_json::from_str(include_str!("fixtures/oracle-successor.json")).unwrap();
    assert_eq!(fixture["sources"], successor["sources"]);
    assert_eq!(fixture["tie_word_embeddings"], false);
    assert_eq!(fixture["vocab"], VOCAB);
    assert_eq!(fixture["hidden"], HIDDEN);
    assert_eq!(fixture["rms_norm_epsilon"], 1e-6);
    assert_eq!(fixture["arrays"].as_object().unwrap().len(), 1 + 4 * 3);
    assert_eq!(fixture["weights"].as_object().unwrap().len(), 3);
    // The binding must be to the very decoder capture already admitted for this profile.
    let decoder: Value = serde_json::from_slice(
        &std::fs::read(
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join(format!("tests/fixtures/moe-decoder-{profile}.json")),
        )
        .unwrap(),
    )
    .unwrap();
    for step in 0..4 {
        let key = format!("layer-39-step-{step}-output");
        assert_eq!(
            fixture["decoder_binding"]["arrays"][&key]["payload_sha256"],
            decoder["arrays"][&key]["payload_sha256"]
        );
    }
    // The checkpoint is genuinely untied: distinct source images, distinct shards.
    let weights = &fixture["weights"];
    assert_ne!(
        weights[EMBED_KEY]["source_sha256"],
        weights[HEAD_KEY]["source_sha256"]
    );
    assert_eq!(
        weights[EMBED_KEY]["source_bytes"],
        weights[HEAD_KEY]["source_bytes"]
    );
    assert_ne!(weights[EMBED_KEY]["shard"], weights[HEAD_KEY]["shard"]);

    let root = checkpoint_root();
    for (name, key) in [
        ("config.json", "config_sha256"),
        ("model.safetensors.index.json", "index_sha256"),
    ] {
        assert_eq!(
            fixture["checkpoint"][key],
            json!(format!(
                "{:x}",
                Sha256::digest(std::fs::read(root.join(name)).unwrap())
            ))
        );
    }
    let captures = PathBuf::from(
        std::env::var("MYCELIUM_MOE_DECODER_CAPTURES")
            .expect("recorded decoder capture root (oracle-f32 / oracle-packed)"),
    )
    .join(format!("oracle-{profile}"));
    let ctx = Context::new(6 * 1024 * 1024 * 1024, 128 * 1024 * 1024).unwrap();
    let checks = run(&ctx, &root, &fixture, &captures, profile);
    ctx.close().unwrap(); // every array handle was dropped inside `run`
    println!(
        "HEAD_COMPLETE {profile} checks={checks} elapsed_s={:.3}",
        started.elapsed().as_secs_f64()
    );
}

fn run(ctx: &Rc<Context>, root: &Path, fixture: &Value, captures: &Path, profile: &str) -> usize {
    let mut checks = 0usize;

    // ---- entry stage: untied embedding rows.
    let embedding = load_weights(ctx, root, fixture, &embedding_weights(), profile);
    assert_eq!(embedding.len(), 1, "entry stage owns only the embedding");
    let ids: Vec<u32> = fixture["embedding_ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| u32::try_from(v.as_u64().unwrap()).unwrap())
        .collect();
    let rows = embed(&ctx.ids(&ids).unwrap(), &embedding).unwrap();
    payload(&fixture["arrays"]["embedding-lookup"], &rows);
    checks += 1;
    assert_eq!(
        embed(&ctx.ids(&ids).unwrap(), &std::collections::BTreeMap::new())
            .err()
            .unwrap()
            .code,
        ErrorCode::Artifact
    );
    let two_rows = ctx.words(&[1, 2], &[2, 1]).unwrap();
    assert_eq!(
        embed(&two_rows, &embedding).err().unwrap().code,
        ErrorCode::MalformedTensor
    );
    drop(embedding);
    ctx.clear_cache().unwrap();

    // ---- final stage: norm + untied head over the recorded layer-39 outputs.
    let mut stage = load_weights(ctx, root, fixture, &head_weights(), profile);
    assert_eq!(stage.len(), 2, "head stage owns only norm and lm_head");
    assert!(!stage.contains_key(EMBED_KEY));
    for step in 0..4 {
        quiet();
        let key = format!("layer-39-step-{step}-output");
        let bound = &fixture["decoder_binding"]["arrays"][&key];
        let shape: Vec<i32> = serde_json::from_value(bound["shape"].clone()).unwrap();
        let bytes = shape.iter().product::<i32>() as u64 * 4;
        let expected = json!({
            "dtype": "float32", "shape": bound["shape"], "payload_bytes": bytes, "payload_sha256": bound["payload_sha256"],
        });
        let data = npy_f32(&captures.join(format!("{key}.npy")), &expected);
        let x = ctx.floats(&data, &shape).unwrap();
        payload(&expected, &x);
        let norm = final_norm(&x, &stage).unwrap();
        payload(
            &fixture["arrays"][format!("step-{step}-final-norm-all")],
            &norm,
        );
        let all = head(&x, &stage, true).unwrap();
        payload(&fixture["arrays"][format!("step-{step}-all-logits")], &all);
        let last = head(&x, &stage, false).unwrap();
        payload(
            &fixture["arrays"][format!("step-{step}-last-logits")],
            &last,
        );
        checks += 4;
        let selected = &fixture["selected_tokens"][step];
        assert_eq!(selected["rows"], shape[1]);
        let flat = all.floats().unwrap();
        let tokens: Vec<u32> = flat
            .chunks_exact(VOCAB as usize)
            .map(|row| select_greedy(row).unwrap())
            .collect();
        assert_eq!(json!(tokens), selected["all_row_tokens"]);
        let last_row = last.floats().unwrap();
        assert_eq!(last_row.len(), VOCAB as usize);
        assert_eq!(
            json!(select_greedy(&last_row).unwrap()),
            selected["last_token"]
        );
        checks += 2;
        println!(
            "HEAD_STEP {profile} step={step} rows={} token={}",
            shape[1], selected["last_token"]
        );
    }

    // ---- negative controls: no tied fallback, no malformed hidden.
    let removed = stage.remove(HEAD_KEY).unwrap();
    let probe = ctx
        .floats(&vec![0.5; HIDDEN as usize], &[1, 1, HIDDEN])
        .unwrap();
    assert_eq!(
        head(&probe, &stage, false).err().unwrap().code,
        ErrorCode::Artifact,
        "a missing lm_head must not fall back to the embedding"
    );
    stage.insert(HEAD_KEY.into(), removed);
    for shape in [[1, 1, HIDDEN - 1], [1, 129, HIDDEN], [2, 1, HIDDEN]] {
        let n = shape.iter().product::<i32>() as usize;
        let bad = ctx.floats(&vec![0.5; n], &shape).unwrap();
        assert_eq!(
            head(&bad, &stage, true).err().unwrap().code,
            ErrorCode::MalformedTensor
        );
    }
    drop(stage);
    checks
}
#[test]
fn qwen36_head_f32() {
    head_component("f32");
}
#[test]
fn qwen36_head_packed() {
    head_component("packed");
}
