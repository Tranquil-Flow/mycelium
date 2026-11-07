// SPDX-License-Identifier: AGPL-3.0-only
//! QW-2 (quiet-window job, never run by the ordinary gates): the whole packed Qwen3.6-35B-A3B model
//! resident as the planned stage chain, greedy decode with the selected token fed back, compared
//! step for step with the oracle recording made by `capture_greedy.py` (selected tokens 64/64, SHA-256
//! of every step's logits and last-stage hidden payload). Stage hops go through the L5 `Activation`
//! byte contract (in-process host copy; NOT a real wire). Weights are hash-verified by the same
//! `load_weights` as every other MoE test. Not model admission.
//!
//! Env: MYCELIUM_MOE_CHECKPOINT (snapshot), QW2_RECORDING (dir holding the recorder's fixture.json),
//! QW2_ALLOCATOR_GIB (native allocator limit for this window run), QW2_OUT (summary JSON path).
#![cfg(feature = "metal")]
use backend_mlx::{Array, Context};
use model::{
    Tensor,
    greedy::{
        Chain, ChainOutput, StageChain, StageIo, StepRecord, first_divergence, generate, hex,
    },
    moe_decoder::{HIDDEN, embedding_weights, head_weights, plan_stage_split, stage_weights},
    moe_stage::MoeStage,
};
use serde_json::{Value, json};
use stage_contract::{Activation, ByteOrder, DType, Layout};
use std::{collections::BTreeMap, path::PathBuf, rc::Rc, time::Instant};

mod common;
use common::{checkpoint_root, load_weights, quiet};

const STAGE_BYTE_CAP: u64 = 6 * 1024 * 1024 * 1024;

fn fixture(name: &str) -> Value {
    serde_json::from_slice(
        &std::fs::read(
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(format!("tests/fixtures/{name}.json")),
        )
        .expect("mandatory recorded fixture"),
    )
    .unwrap()
}
fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("{name} required"))
}
struct Io(Rc<Context>);
impl StageIo<Array> for Io {
    fn ids(&self, ids: &[u32]) -> stage_contract::Result<Array> {
        self.0.ids(ids)
    }
    fn floats(&self, t: &Array) -> stage_contract::Result<Vec<f32>> {
        t.floats()
    }
    /// Export as the L5 contract carries it and import on the receiving side.
    fn hop(&self, hidden: &Array) -> stage_contract::Result<Array> {
        let shape = hidden.shape();
        let activation = Activation {
            shape: [shape[0] as u32, shape[1] as u32, shape[2] as u32],
            dtype: DType::F32,
            order: ByteOrder::Little,
            layout: Layout::BatchSequenceHidden,
            bytes: hidden
                .floats()?
                .iter()
                .flat_map(|v| v.to_le_bytes())
                .collect(),
        };
        activation.validate(HIDDEN as u32, u64::MAX)?;
        let values: Vec<f32> = activation
            .bytes
            .chunks_exact(4)
            .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
            .collect();
        self.0.floats(&values, &shape)
    }
}
/// Pressure/demo/cert-waiter guard before EVERY step, plus per-step timing.
struct Guarded<C> {
    inner: C,
    seconds: Vec<f64>,
}
impl<C: Chain> Chain for Guarded<C> {
    fn step(&mut self, ids: &[u32], position: i32) -> stage_contract::Result<ChainOutput> {
        quiet();
        let start = Instant::now();
        let out = self.inner.step(ids, position);
        self.seconds.push(start.elapsed().as_secs_f64());
        out
    }
    fn reset(&mut self) {
        self.inner.reset();
    }
}
fn unhex(text: &str) -> [u8; 32] {
    let mut out = [0_u8; 32];
    assert_eq!(text.len(), 64);
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&text[2 * i..2 * i + 2], 16).unwrap();
    }
    out
}

#[test]
#[ignore = "QW-2: quiet-window job only (34.5 GiB resident, ~1 h weight hashing); run via L8-evidence/qw2-run.sh"]
fn qwen36_greedy_64_tokens_equal_the_oracle_recording() {
    quiet();
    let started = Instant::now();
    let recording: Value = serde_json::from_slice(
        &std::fs::read(PathBuf::from(env("QW2_RECORDING")).join("fixture.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(recording["schema"], "l8-moe-greedy64-v1");
    assert_eq!(recording["profile"], "packed");
    assert_eq!(
        recording["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let prompt: Vec<u32> = serde_json::from_value(recording["prompt_ids"].clone()).unwrap();
    let new_tokens = recording["new_tokens"].as_u64().unwrap() as usize;
    assert_eq!(new_tokens, 64, "QW-2 is 64 selections");
    let oracle: Vec<StepRecord> = recording["steps"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| StepRecord {
            index: s["index"].as_u64().unwrap() as usize,
            rows: s["rows"].as_u64().unwrap() as usize,
            position: s["position"].as_i64().unwrap() as i32,
            token: s["token"].as_u64().unwrap() as u32,
            logits_sha256: unhex(s["logits_sha256"].as_str().unwrap()),
            hidden_sha256: Some(unhex(s["hidden_sha256"].as_str().unwrap())),
        })
        .collect();
    assert_eq!(oracle.len(), new_tokens);

    let gib: usize = env("QW2_ALLOCATOR_GIB").parse().unwrap();
    let ctx = Context::new(gib * 1024 * 1024 * 1024, 128 * 1024 * 1024).unwrap();
    let root = checkpoint_root();
    let decoder = fixture("moe-decoder-packed");
    let head_fixture = fixture("moe-head-packed");
    // The planned multi-process split (>= 6 packed stages under the 6 GiB per-worker cap), here
    // resident in one process so the hop path and stage ownership are the ones that will ship.
    let plan = plan_stage_split(STAGE_BYTE_CAP, true).unwrap();
    let cap = (prompt.len() + new_tokens) as u32;
    let mut stages = Vec::new();
    for p in &plan {
        quiet();
        let all = stage_weights(p.layers.clone(), p.embedding, p.head).unwrap();
        let heads: BTreeMap<String, Vec<i32>> = embedding_weights()
            .into_iter()
            .chain(head_weights())
            .collect();
        let (from_head, from_layers): (BTreeMap<_, _>, BTreeMap<_, _>) = all
            .into_iter()
            .partition(|(name, _)| heads.contains_key(name));
        let mut weights = load_weights(&ctx, &root, &decoder, &from_layers, "packed");
        weights.extend(load_weights(
            &ctx,
            &root,
            &head_fixture,
            &from_head,
            "packed",
        ));
        stages.push(MoeStage::new(p.layers.clone(), p.embedding, p.head, cap, weights).unwrap());
        println!(
            "STAGE_RESIDENT layers={:?} embed={} head={} planned_bytes={} elapsed_s={:.1}",
            p.layers,
            p.embedding,
            p.head,
            p.resident_bytes,
            started.elapsed().as_secs_f64()
        );
    }
    let stage_count = stages.len();
    let mut chain = Guarded {
        inner: StageChain::new(stages, Io(Rc::clone(&ctx))).unwrap(),
        seconds: Vec::new(),
    };
    let run = generate(&mut chain, &prompt, new_tokens, &[], cap as usize).unwrap();
    let divergence = first_divergence(&oracle, &run.steps);
    let equal_tokens = run
        .steps
        .iter()
        .zip(&oracle)
        .filter(|(a, b)| a.token == b.token)
        .count();
    let summary = json!({
        "schema": "l8-moe-greedy64-rust-summary-v1",
        "stages": stage_count,
        "steps": run.steps.len(),
        "selected_token_equal": equal_tokens,
        "first_divergence": divergence,
        "tokens": run.tokens(),
        "logits_sha256": run.steps.iter().map(|s| hex(&s.logits_sha256)).collect::<Vec<_>>(),
        "step_seconds": chain.seconds,
        "elapsed_s": started.elapsed().as_secs_f64(),
    });
    std::fs::write(env("QW2_OUT"), serde_json::to_vec_pretty(&summary).unwrap()).unwrap();
    println!("GREEDY64_RESULT equal={equal_tokens}/{new_tokens} first_divergence={divergence:?}");
    assert_eq!(divergence, None, "first diverging step");
    assert_eq!(equal_tokens, new_tokens);
    ctx.close().unwrap();
}
