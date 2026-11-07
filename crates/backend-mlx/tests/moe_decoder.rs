// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "metal")]
use backend_mlx::Context;
use stage_contract::ErrorCode;
use std::collections::BTreeMap;

use backend_mlx::Array;
use model::{Tensor, moe_decoder::DecoderLayer};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{path::PathBuf, time::Instant};

mod common;
use common::{load_weights, payload, quiet};
fn decoder(profile: &str) {
    quiet();
    let fixture: Value = serde_json::from_slice(
        &std::fs::read(
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join(format!("tests/fixtures/moe-decoder-{profile}.json")),
        )
        .expect("mandatory recorded MoE fixture"),
    )
    .unwrap();
    assert_eq!(fixture["schema"], "l8-moe-decoder-component-v1");
    assert_eq!(
        fixture["scope"],
        "streamed-hidden-decoder-not-resident-stage-model-or-transport-admission"
    );
    assert_eq!(
        fixture["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    assert_eq!(fixture["profile"], profile);
    let successor: Value =
        serde_json::from_str(include_str!("fixtures/oracle-successor.json")).unwrap();
    assert_eq!(fixture["sources"], successor["sources"]);
    assert_eq!(fixture["layers"], 40);
    assert_eq!(fixture["positions"], json!([0, 3, 4, 0]));
    assert_eq!(fixture["rows"], json!([3, 1, 1, 5]));
    assert_eq!(fixture["host_split_boundaries"], json!([1, 3, 20, 39]));
    assert_eq!(fixture["arrays"].as_object().unwrap().len(), 4 + 40 * 4 * 6);
    assert_eq!(
        fixture["weights"].as_object().unwrap().len(),
        30 * 18 + 10 * 15
    );
    let root = PathBuf::from(
        std::env::var("MYCELIUM_MOE_CHECKPOINT").expect("cached snapshot; never download"),
    );
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
    let started = Instant::now();
    let ctx = Context::new(6 * 1024 * 1024 * 1024, 128 * 1024 * 1024).unwrap();
    let base: Vec<f32> = (0..5 * 2048)
        .map(|i| ((i % 97 - 48) as f32) / 64.)
        .collect();
    let initial = || {
        [(0, 3), (3, 4), (4, 5), (0, 5)]
            .into_iter()
            .map(|(a, b)| {
                ctx.floats(&base[a * 2048..b * 2048], &[1, (b - a) as i32, 2048])
                    .unwrap()
            })
            .collect::<Vec<_>>()
    };
    let mut contiguous = initial();
    let mut split = initial();
    for (step, x) in contiguous.iter().enumerate() {
        payload(&fixture["arrays"][format!("input-{step}")], x);
    }
    let mut checks = 4usize;
    for layer in 0..40 {
        quiet();
        let load_start = Instant::now();
        let weights = load_weights(
            &ctx,
            &root,
            &fixture,
            &model::moe_decoder::layer_weights(layer).unwrap(),
            profile,
        );
        let load_s = load_start.elapsed().as_secs_f64();
        let compute_start = Instant::now();
        let mut a = DecoderLayer::new(layer, 5).unwrap();
        let mut b = DecoderLayer::new(layer, 5).unwrap();
        let mut next_a = Vec::new();
        let mut next_b = Vec::new();
        for (step, position) in [0, 3, 4, 0].into_iter().enumerate() {
            quiet();
            if step == 3 {
                a.reset();
                b.reset();
                assert_eq!(a.position(), 0);
                assert!(a.state().is_none());
            }
            // Wrong-position refusal must precede even missing artifact access.
            assert_eq!(
                a.run(&contiguous[step], &BTreeMap::new(), position + 1)
                    .err()
                    .unwrap()
                    .code,
                ErrorCode::Position
            );
            assert_eq!(a.position(), position);
            let ra = a.run(&contiguous[step], &weights, position).unwrap();
            let rb = b.run(&split[step], &weights, position).unwrap();
            let prefix = format!("layer-{layer}-step-{step}");
            let (sa, sb) = a.state().unwrap();
            let (ta, tb) = b.state().unwrap();
            for (key, x, y) in [
                ("output", &ra.output, &rb.output),
                ("probabilities", &ra.probabilities, &rb.probabilities),
                ("scores", &ra.scores, &rb.scores),
                ("indices", &ra.indices, &rb.indices),
                ("state-0", sa, ta),
                ("state-1", sb, tb),
            ] {
                eprintln!("CHECK {profile} {prefix}-{key}");
                payload(&fixture["arrays"][format!("{prefix}-{key}")], x);
                payload(&fixture["arrays"][format!("{prefix}-{key}")], y);
                checks += 1;
            }
            assert_eq!(
                a.run(&contiguous[step], &weights, position)
                    .err()
                    .unwrap()
                    .code,
                ErrorCode::Position
            );
            assert_eq!(
                a.run(&contiguous[step], &weights, 0).err().unwrap().code,
                ErrorCode::Position
            );
            if step == 0 {
                // A valid next position with missing weights cannot mutate committed state.
                assert_eq!(
                    a.run(&contiguous[1], &BTreeMap::new(), 3)
                        .err()
                        .unwrap()
                        .code,
                    ErrorCode::MalformedTensor
                );
                assert_eq!(a.position(), 3);
            }
            payload(
                &fixture["arrays"][format!("{prefix}-state-0")],
                a.state().unwrap().0,
            );
            payload(
                &fixture["arrays"][format!("{prefix}-state-1")],
                a.state().unwrap().1,
            );
            next_a.push(ra.output);
            // Host export/import only at declared boundaries. No wire claim.
            next_b.push(if [1, 3, 20, 39].contains(&(layer + 1)) {
                ctx.floats(&rb.output.floats().unwrap(), &rb.output.shape())
                    .unwrap()
            } else {
                rb.output
            });
        }
        assert_eq!(
            a.run(&contiguous[1], &weights, 5).err().unwrap().code,
            ErrorCode::ResourceCap
        );
        assert_eq!(a.position(), 5);
        drop(a);
        drop(b);
        drop(weights);
        contiguous = next_a;
        split = next_b;
        ctx.synchronize().unwrap();
        ctx.clear_cache().unwrap();
        let memory = ctx.memory_stats().unwrap();
        // After each layer only the small boundary activations may remain.
        assert!(
            memory.0 < 4 * 1024 * 1024,
            "layer retained weight/state allocations: {memory:?}"
        );
        eprintln!(
            "LAYER_COMPLETE {}",
            json!({"profile":profile,"layer":layer,"load_s":load_s,"compute_compare_s":compute_start.elapsed().as_secs_f64(),"active_bytes":memory.0,"peak_bytes":memory.1})
        );
    }
    assert_eq!(checks, fixture["arrays"].as_object().unwrap().len());
    drop(contiguous);
    drop(split);
    ctx.synchronize().unwrap();
    ctx.clear_cache().unwrap();
    assert_eq!(ctx.memory_stats().unwrap().0, 0);
    eprintln!(
        "DECODER_COMPLETE {}",
        json!({"profile":profile,"checks_per_chain":checks,"chains":2,"elapsed_s":started.elapsed().as_secs_f64(),"memory":ctx.memory_stats().unwrap()})
    );
    ctx.close().unwrap();
}
#[test]
fn qwen36_decoder_chain_f32() {
    decoder("f32");
}
#[test]
fn qwen36_decoder_chain_packed() {
    decoder("packed");
}

#[test]
fn moe_cursor_refuses_malformed_caps_and_missing_weights_transactionally() {
    let ctx = Context::new(64 * 1024 * 1024, 0).unwrap();
    for cap in [0, u32::MAX] {
        assert_eq!(
            DecoderLayer::<Array>::new(0, cap).err().unwrap().code,
            ErrorCode::ResourceCap
        );
    }
    assert_eq!(
        DecoderLayer::<Array>::new(40, 5).err().unwrap().code,
        ErrorCode::InvalidDescriptor
    );
    for layer in [0, 3, 39] {
        let mut d = DecoderLayer::new(layer, 5).unwrap();
        for (shape, values) in [
            (vec![2048], vec![0.; 2048]),
            (vec![2, 1, 2048], vec![0.; 4096]),
            (vec![1, 1, 1024], vec![0.; 1024]),
        ] {
            let x = ctx.floats(&values, &shape).unwrap();
            assert_eq!(
                d.run(&x, &BTreeMap::new(), 0).err().unwrap().code,
                ErrorCode::MalformedTensor
            );
        }
        let x = ctx.floats(&[0.; 2048], &[1, 1, 2048]).unwrap();
        assert_eq!(
            d.run(&x, &BTreeMap::new(), -1).err().unwrap().code,
            ErrorCode::Position
        );
        assert_eq!(
            d.run(&x, &BTreeMap::new(), 0).err().unwrap().code,
            ErrorCode::MalformedTensor
        );
        assert_eq!(d.position(), 0);
        assert!(d.state().is_none());
    }
}

#[test]
fn moe_layer_index_is_checked_before_weights() {
    let ctx = Context::new(64 * 1024 * 1024, 0).unwrap();
    let x = ctx.floats(&[0.; 2048], &[1, 1, 2048]).unwrap();
    for layer in [40, u32::MAX] {
        assert_eq!(
            model::moe::qwen36_block(&x, &BTreeMap::new(), layer)
                .err()
                .unwrap()
                .code,
            ErrorCode::InvalidDescriptor,
        );
    }
    assert_eq!(
        model::moe::qwen36_block(&x, &BTreeMap::new(), 39)
            .err()
            .unwrap()
            .code,
        ErrorCode::Artifact
    );
}
