// SPDX-License-Identifier: AGPL-3.0-only
//! Resident Qwen3.6-35B-A3B stages (`model::moe_stage::MoeStage`) against the pinned v0.1.4
//! oracle (f4e366e9): entry stage (tokens -> untied embedding -> layer), a mid stage and a
//! final stage (layers -> last-row final norm -> untied head), with stage-to-stage handoff over
//! the L5 `Activation` byte contract (in-process host copy; NOT a real wire/QUIC transport).
//! Recorded boundary inputs come from the existing decoder captures. Not model admission.
#![cfg(feature = "metal")]
use backend_mlx::{Array, Context};
use model::{
    Tensor, Weight,
    moe_decoder::{self, EMBED_KEY, HEAD_KEY, HIDDEN, head_weights},
    moe_stage::{MoeStage, StageInput},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use stage_contract::{Activation, ByteOrder, DType, ErrorCode, Layout};
use std::{collections::BTreeMap, path::PathBuf, rc::Rc, time::Instant};

mod common;
use common::{checkpoint_root, load_weights, npy_f32, payload, quiet};

type Weights = BTreeMap<String, Weight<Array>>;
const POSITIONS: [i32; 4] = [0, 3, 4, 0];
const ROWS: [i32; 4] = [3, 1, 1, 5];
const CAP: u32 = 5;

fn fixture(name: &str) -> Value {
    serde_json::from_slice(
        &std::fs::read(
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(format!("tests/fixtures/{name}.json")),
        )
        .expect("mandatory recorded fixture"),
    )
    .unwrap()
}
fn env_dir(name: &str) -> PathBuf {
    PathBuf::from(std::env::var(name).unwrap_or_else(|_| panic!("{name} required")))
}
/// The stage-boundary activation exactly as the L5 contract carries it.
fn export(x: &Array) -> Activation {
    let shape = x.shape();
    let activation = Activation {
        shape: [shape[0] as u32, shape[1] as u32, shape[2] as u32],
        dtype: DType::F32,
        order: ByteOrder::Little,
        layout: Layout::BatchSequenceHidden,
        bytes: x
            .floats()
            .unwrap()
            .iter()
            .flat_map(|v| v.to_le_bytes())
            .collect(),
    };
    activation.validate(HIDDEN as u32, u64::MAX).unwrap();
    activation
}
fn import(ctx: &Rc<Context>, a: &Activation) -> Array {
    a.validate(HIDDEN as u32, u64::MAX).unwrap();
    let values: Vec<f32> = a
        .bytes
        .chunks_exact(4)
        .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
        .collect();
    ctx.floats(
        &values,
        &[a.shape[0] as i32, a.shape[1] as i32, a.shape[2] as i32],
    )
    .unwrap()
}
/// Both committed state tensors of `layer` equal the fixture's recorded hashes.
fn check_state(stage: &MoeStage<Array>, fixture: &Value, layer: u32, step: usize) -> usize {
    let (a, b) = stage.layer_state(layer).expect("committed state");
    for (k, tensor) in [a, b].into_iter().enumerate() {
        payload(
            &fixture["arrays"][format!("layer-{layer}-step-{step}-state-{k}")],
            tensor,
        );
    }
    2
}
fn only(
    all: &BTreeMap<String, Vec<i32>>,
    keep: impl Fn(&str) -> bool,
) -> BTreeMap<String, Vec<i32>> {
    all.iter()
        .filter(|(k, _)| keep(k))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}
fn flat(ctx: &Rc<Context>, shape: [i32; 3]) -> Array {
    ctx.floats(&vec![0.5; shape.iter().product::<i32>() as usize], &shape)
        .unwrap()
}

enum Feed<'a> {
    Tokens(&'a [Vec<u32>]),
    Hidden(&'a dyn Fn(usize) -> Array),
}
/// Run the four recorded steps (prefill, two decodes, reset + 5-row replay) through `stage`,
/// returning the number of exact comparisons and the exported stage-boundary activations.
fn drive(
    ctx: &Rc<Context>,
    stage: &mut MoeStage<Array>,
    fixture: &Value,
    feed: Feed,
    head: Option<&Value>,
) -> (usize, Vec<Activation>) {
    let range = stage.layers();
    let last = range.end - 1;
    let (mut checks, mut exported) = (0, Vec::new());
    for (step, position) in POSITIONS.into_iter().enumerate() {
        quiet();
        if step == 3 {
            stage.reset();
            assert_eq!(stage.position(), 0);
            assert!(stage.layer_state(range.start).is_none());
        }
        let out = match &feed {
            Feed::Tokens(ids) => {
                let ids = ctx.ids(&ids[step]).unwrap();
                stage.run(StageInput::Tokens(&ids), position).unwrap()
            }
            Feed::Hidden(input) => {
                let x = input(step);
                assert_eq!(x.shape()[1], ROWS[step]);
                stage.run(StageInput::Hidden(&x), position).unwrap()
            }
        };
        payload(
            &fixture["arrays"][format!("layer-{last}-step-{step}-output")],
            &out.hidden,
        );
        checks += 1;
        for layer in range.clone() {
            checks += check_state(stage, fixture, layer, step);
        }
        match (head, &out.logits) {
            (Some(h), Some(logits)) => {
                payload(&h["arrays"][format!("step-{step}-last-logits")], logits);
                let token = moe_decoder::select_greedy(&logits.floats().unwrap()).unwrap();
                assert_eq!(json!(token), h["selected_tokens"][step]["last_token"]);
                checks += 2;
            }
            (None, None) => {}
            _ => panic!("logits belong to the head stage only"),
        }
        exported.push(export(&out.hidden));
    }
    assert_eq!(stage.position(), CAP as i32);
    (checks, exported)
}

fn stages(profile: &str) {
    quiet();
    let started = Instant::now();
    let packed = profile == "packed";
    // A resident FP32 layer is ~3.46 GB: two cannot share the 6 GiB allocator, so FP32 stages
    // own one layer; packed stages own two (multi-layer atomic commit is then exercised).
    let per_stage: u32 = if packed { 2 } else { 1 };
    let decoder = fixture(&format!("moe-decoder-{profile}"));
    let head_fixture = fixture(&format!("moe-head-{profile}"));
    let entry = fixture(&format!("moe-entry-{profile}"));
    assert_eq!(entry["schema"], "l8-moe-entry-stage-component-v1");
    assert_eq!(
        entry["scope"],
        "entry-embedding-layers-0-1-not-resident-stage-model-or-transport-admission"
    );
    assert_eq!(
        entry["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    assert_eq!(entry["profile"], profile);
    let successor: Value =
        serde_json::from_str(include_str!("fixtures/oracle-successor.json")).unwrap();
    assert_eq!(entry["sources"], successor["sources"]);
    assert_eq!(entry["positions"], json!(POSITIONS));
    assert_eq!(entry["rows"], json!(ROWS));
    let step_ids: Vec<Vec<u32>> = serde_json::from_value(entry["step_ids"].clone()).unwrap();
    assert_eq!(
        step_ids.iter().map(Vec::len).collect::<Vec<_>>(),
        [3, 1, 1, 5]
    );
    let root = checkpoint_root();
    let ctx = Context::new(6 * 1024 * 1024 * 1024, 128 * 1024 * 1024).unwrap();
    let captures = env_dir("MYCELIUM_MOE_DECODER_CAPTURES").join(format!("oracle-{profile}"));
    let mut checks = 0;

    // ---- A. entry stage [0,1)+embedding -> Activation bytes -> stage [1,2).
    let handoffs = {
        let wanted = moe_decoder::stage_weights(0..1, true, false).unwrap();
        let mut stage = MoeStage::new(
            0..1,
            true,
            false,
            CAP,
            load_weights(&ctx, &root, &entry, &wanted, profile),
        )
        .unwrap();
        // Typed refusals precede any state change.
        let ids = ctx.ids(&[9707, 11, 1879]).unwrap();
        let hidden = flat(&ctx, [1, 3, HIDDEN]);
        let wrong = |r: Result<_, stage_contract::Error>| r.err().unwrap().code;
        assert_eq!(
            wrong(stage.run(StageInput::Tokens(&ids), 1)),
            ErrorCode::Position
        );
        assert_eq!(
            wrong(stage.run(StageInput::Hidden(&hidden), 0)),
            ErrorCode::MalformedTensor
        );
        assert_eq!(
            (stage.position(), stage.layer_state(0).is_none()),
            (0, true)
        );
        let (n, out) = drive(&ctx, &mut stage, &entry, Feed::Tokens(&step_ids), None);
        checks += n;
        // The replay filled the 5-token cap: one more row refuses and commits nothing.
        let more = ctx.ids(&[1]).unwrap();
        assert_eq!(
            wrong(stage.run(StageInput::Tokens(&more), 5)),
            ErrorCode::ResourceCap
        );
        assert_eq!(stage.position(), 5);
        checks += check_state(&stage, &entry, 0, 3);
        out
    };
    ctx.clear_cache().unwrap();
    {
        let wanted = moe_decoder::stage_weights(1..2, false, false).unwrap();
        let mut stage = MoeStage::new(
            1..2,
            false,
            false,
            CAP,
            load_weights(&ctx, &root, &entry, &wanted, profile),
        )
        .unwrap();
        let input = |step: usize| import(&ctx, &handoffs[step]);
        let (n, _) = drive(&ctx, &mut stage, &entry, Feed::Hidden(&input), None);
        checks += n;
        // What crossed the hop hashes like the oracle's layer-0 output.
        for (step, a) in handoffs.iter().enumerate() {
            let want = &entry["arrays"][format!("layer-0-step-{step}-output")];
            assert_eq!(
                want["payload_sha256"],
                json!(format!("{:x}", Sha256::digest(&a.bytes)))
            );
            checks += 1;
        }
    }
    ctx.clear_cache().unwrap();

    // ---- B. recorded boundary inputs -> mid stage and final stage (norm + untied head).
    let boundary = |layer: u32| {
        let captures = captures.clone();
        let decoder = decoder.clone();
        let ctx = ctx.clone();
        move |step: usize| -> Array {
            let key = format!("layer-{layer}-step-{step}-output");
            let bound = &decoder["arrays"][&key];
            let shape: Vec<i32> = serde_json::from_value(bound["shape"].clone()).unwrap();
            let x = ctx
                .floats(
                    &npy_f32(&captures.join(format!("{key}.npy")), bound),
                    &shape,
                )
                .unwrap();
            payload(bound, &x);
            x
        }
    };
    {
        let range = 19..19 + per_stage;
        let wanted = moe_decoder::stage_weights(range.clone(), false, false).unwrap();
        let weights = load_weights(&ctx, &root, &decoder, &wanted, profile);
        let mut stage = MoeStage::new(range, false, false, CAP, weights).unwrap();
        let input = boundary(18);
        checks += drive(&ctx, &mut stage, &decoder, Feed::Hidden(&input), None).0;
    }
    ctx.clear_cache().unwrap();
    {
        let range = 40 - per_stage..40;
        let all = moe_decoder::stage_weights(range.clone(), false, true).unwrap();
        let heads = head_weights();
        let mut weights: Weights = load_weights(
            &ctx,
            &root,
            &decoder,
            &only(&all, |k| !heads.contains_key(k)),
            profile,
        );
        weights.extend(load_weights(&ctx, &root, &head_fixture, &heads, profile));
        // Stage-only residency: a tied alias (extra embedding), a missing untied head, or an
        // unassigned layer is refused before any compute.
        let inventory = |w: &Weights| {
            MoeStage::verify_inventory(range.clone(), false, true, w).map_err(|e| e.code)
        };
        assert_eq!(inventory(&weights), Ok(()));
        weights.insert(EMBED_KEY.into(), Weight::Float(flat(&ctx, [1, 1, 1])));
        assert_eq!(inventory(&weights), Err(ErrorCode::MalformedTensor));
        weights.remove(EMBED_KEY);
        let head = weights.remove(HEAD_KEY).unwrap();
        assert_eq!(inventory(&weights), Err(ErrorCode::MalformedTensor));
        weights.insert(HEAD_KEY.into(), head);
        let mut stage = MoeStage::new(range, false, true, CAP, weights).unwrap();
        let input = boundary(40 - per_stage - 1);
        checks += drive(
            &ctx,
            &mut stage,
            &decoder,
            Feed::Hidden(&input),
            Some(&head_fixture),
        )
        .0;
        // Cap refusal leaves the committed replay state untouched.
        let one = flat(&ctx, [1, 1, HIDDEN]);
        let refused = stage.run(StageInput::Hidden(&one), 5).err().unwrap().code;
        assert_eq!(refused, ErrorCode::ResourceCap);
        assert_eq!(stage.position(), 5);
        checks += check_state(&stage, &decoder, 39, 3);
    }
    println!(
        "STAGES_COMPLETE {profile} per_stage={per_stage} checks={checks} elapsed_s={:.3}",
        started.elapsed().as_secs_f64()
    );
    ctx.close().unwrap();
}

#[test]
fn qwen36_stages_packed() {
    stages("packed");
}
#[test]
fn qwen36_stages_f32() {
    stages("f32");
}
