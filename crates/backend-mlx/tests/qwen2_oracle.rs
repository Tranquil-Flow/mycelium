// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "metal")]
use backend_mlx::{
    Context, MlxBackend,
    admission::{admit, qwen2_descriptor},
    payload_id,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use stage_contract::*;
use std::{path::PathBuf, rc::Rc, time::Instant};
fn quiet() {
    let home = std::env::var("HOME").unwrap();
    let root = PathBuf::from(home).join("mycelium-demo-run");
    if root.exists() {
        for item in std::fs::read_dir(root).unwrap() {
            assert!(
                !item
                    .unwrap()
                    .file_name()
                    .to_string_lossy()
                    .ends_with(".busy"),
                "host hold: stop native test"
            );
        }
    }
    let rc = std::process::Command::new("pgrep")
        .args(["-f", "[P]ARENT-cert-.*-when-quiet"])
        .status()
        .unwrap();
    assert_eq!(rc.code(), Some(1), "host hold or pgrep error");
}
fn check(fixture: &Value, name: &str, bytes: &[u8], shape: &[u32]) {
    let expected = &fixture["arrays"][name];
    assert!(expected.is_object(), "missing oracle {name}");
    assert_eq!(json!(shape), expected["shape"], "shape {name}");
    assert_eq!(
        bytes.len() as u64,
        expected["payload_bytes"].as_u64().unwrap(),
        "bytes {name}"
    );
    assert_eq!(
        format!("{:x}", Sha256::digest(bytes)),
        expected["payload_sha256"].as_str().unwrap(),
        "bit-exact FP32 words {name}"
    );
}
fn binding(d: StageDescriptor) -> Binding {
    Binding {
        request: [3; 32],
        assignment: [4; 32],
        load_generation: 1,
        descriptor: d,
    }
}
fn step(binding: &Binding, position: u32, input: Input) -> Step {
    Step {
        binding: binding.clone(),
        state_generation: 0,
        position,
        payload: payload_id(&input),
        input,
    }
}
fn cap() -> Limits {
    Limits {
        weight_bytes: 4_000_000_000,
        state_bytes: 10_000_000,
        scratch_bytes: 1_024_000_000,
        max_tokens: 128,
    }
}

/// L8 / F14 / F16. Real cached checkpoint, same-host two-stage worker seam.
/// No network, wire codec, tokenizer, process isolation or new oracle invocation.
#[test]
fn qwen2_two_stage_prefill_63_decodes_exact_and_refusals() {
    quiet();
    let model = PathBuf::from(std::env::var("MYCELIUM_QWEN2_CHECKPOINT").expect(
        "BLOCKED: set MYCELIUM_QWEN2_CHECKPOINT to the pinned cached safetensors; no auto-download",
    ));
    let fixture: Value = serde_json::from_str(include_str!("fixtures/qwen2-f32.json")).unwrap();
    let ctx = Context::new(6 * 1024 * 1024 * 1024, 256 * 1024 * 1024).unwrap();
    let mut backend = MlxBackend::new(Rc::clone(&ctx));
    assert_eq!(
        backend.capabilities().profiles,
        vec![
            NumericsProfile::MlxExplicitF32,
            NumericsProfile::MlxPackedInt8
        ]
    );
    assert!(!backend.capabilities().wire_export);
    let artifact = Artifacts { checkpoint: model };
    let da = qwen2_descriptor(0..8, true, false).unwrap();
    let db = qwen2_descriptor(8..24, false, true).unwrap();
    let mut too_small = cap();
    too_small.weight_bytes = 1;
    assert_eq!(
        backend
            .prepare(
                da.clone(),
                &Artifacts {
                    checkpoint: PathBuf::from("missing-file-must-not-be-opened")
                },
                too_small,
                1
            )
            .err()
            .unwrap()
            .code,
        ErrorCode::ResourceCap
    );
    let load = Instant::now();
    let mut a = backend.prepare(da.clone(), &artifact, cap(), 1).unwrap();
    let after_a = ctx.memory_stats().unwrap();
    let mut b = backend.prepare(db.clone(), &artifact, cap(), 1).unwrap();
    let after_b = ctx.memory_stats().unwrap();
    let load_ms = load.elapsed().as_secs_f64() * 1000.;
    assert_eq!(a.loaded_bytes(), admit(&da, cap()).unwrap());
    assert_eq!(b.loaded_bytes(), admit(&db, cap()).unwrap());
    assert!(
        a.loaded_names()
            .iter()
            .all(|n| *n == "model.embed_tokens.weight"
                || (0..8).any(|i| n.starts_with(&format!("model.layers.{i}."))))
    );
    assert!(
        b.loaded_names()
            .iter()
            .all(|n| *n == "model.embed_tokens.weight"
                || *n == "model.norm.weight"
                || (8..24).any(|i| n.starts_with(&format!("model.layers.{i}."))))
    );
    let ba = binding(da);
    let bb = binding(db);
    let mut insufficient_scratch = cap();
    insufficient_scratch.scratch_bytes = 1;
    assert_eq!(
        a.new_session(ba.clone(), insufficient_scratch)
            .err()
            .expect("session scratch cap must refuse")
            .code,
        ErrorCode::ResourceCap
    );
    let mut sa = a.new_session(ba.clone(), cap()).unwrap();
    let mut sb = b.new_session(bb.clone(), cap()).unwrap();
    assert_eq!(a.close().unwrap_err().code, ErrorCode::Cleanup);
    let mut wrong = ba.clone();
    wrong.load_generation = 2;
    assert_eq!(
        b.new_session(wrong, cap()).err().unwrap().code,
        ErrorCode::ResourceCap
    );
    // Refusals must leave committed position/state untouched.
    let mut bad = step(&ba, 0, Input::Tokens(vec![151936]));
    assert_eq!(
        a.prefill(&mut sa, bad.clone()).unwrap_err().code,
        ErrorCode::MalformedTensor
    );
    bad = step(&ba, 0, Input::Tokens(vec![1]));
    bad.binding.request = [7; 32];
    assert_eq!(
        a.prefill(&mut sa, bad).unwrap_err().code,
        ErrorCode::Binding
    );
    let mut bad = step(&ba, 0, Input::Tokens(vec![1]));
    bad.payload = [0; 32];
    assert_eq!(
        a.prefill(&mut sa, bad).unwrap_err().code,
        ErrorCode::Binding
    );
    let too_long = step(&ba, 0, Input::Tokens(vec![1; 129]));
    assert_eq!(
        a.prefill(&mut sa, too_long).unwrap_err().code,
        ErrorCode::ResourceCap
    );
    let malformed = Activation {
        shape: [1, 1, 896],
        dtype: DType::F32,
        order: ByteOrder::Little,
        layout: Layout::BatchSequenceHidden,
        bytes: vec![0; 4],
    };
    assert_eq!(
        b.prefill(&mut sb, step(&bb, 0, Input::Hidden(malformed)))
            .unwrap_err()
            .code,
        ErrorCode::MalformedTensor
    );
    assert_eq!(sa.position(), 0);
    assert!(a.kv_snapshot(&sa).unwrap().is_empty());
    assert_eq!(sb.position(), 0);
    assert!(b.kv_snapshot(&sb).unwrap().is_empty());
    let mut ids: Vec<u32> = fixture["workload"]["prompt_ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_u64().unwrap() as u32)
        .collect();
    let mut pos = 0;
    let mut times = Vec::new();
    let mut arrays = 0;
    for i in 0..64 {
        quiet();
        assert_eq!(
            json!(ids),
            fixture["feeds"][i],
            "independent greedy feed {i}"
        );
        let input = step(&ba, pos, Input::Tokens(ids.clone()));
        let duplicate = input.clone();
        let t = Instant::now();
        let oa = if i == 0 {
            a.prefill(&mut sa, input)
        } else {
            a.decode(&mut sa, input)
        }
        .unwrap();
        let stage_a_ms = t.elapsed().as_secs_f64() * 1000.;
        check(
            &fixture,
            &format!("stage8-{i}.npy"),
            &oa.hidden.bytes,
            &oa.hidden.shape,
        );
        arrays += 1;
        let input = step(&bb, pos, Input::Hidden(oa.hidden));
        let t = Instant::now();
        let ob = if i == 0 {
            b.prefill(&mut sb, input)
        } else {
            b.decode(&mut sb, input)
        }
        .unwrap();
        let stage_b_head_ms = t.elapsed().as_secs_f64() * 1000.;
        check(
            &fixture,
            &format!("stage24-{i}.npy"),
            &ob.hidden.bytes,
            &ob.hidden.shape,
        );
        arrays += 1;
        let logits = ob.logits.unwrap();
        check(
            &fixture,
            &format!("logits-{i}.npy"),
            &logits.bytes,
            &[151936],
        );
        arrays += 1;
        let values: Vec<f32> = logits
            .bytes
            .chunks_exact(4)
            .map(|v| f32::from_le_bytes([v[0], v[1], v[2], v[3]]))
            .collect();
        let token = values
            .iter()
            .enumerate()
            .max_by(|(i, x), (j, y)| x.total_cmp(y).then(j.cmp(i)))
            .unwrap()
            .0 as u32;
        assert_eq!(u64::from(token), fixture["tokens"][i].as_u64().unwrap());
        pos += ids.len() as u32;
        ids = vec![token];
        assert_eq!(oa.committed_position, pos);
        assert_eq!(ob.committed_position, pos);
        assert_eq!(
            a.decode(&mut sa, duplicate).unwrap_err().code,
            ErrorCode::Position
        );
        assert_eq!(sa.position(), pos);
        if i < 63 {
            for (worker, session) in [(&a, &sa), (&b, &sb)] {
                for (layer, shape, k, v) in worker.kv_snapshot(session).unwrap() {
                    let shape: Vec<u32> = shape.iter().map(|d| *d as u32).collect();
                    check(&fixture, &format!("kv-{layer}-k-after-{pos}"), &k, &shape);
                    check(&fixture, &format!("kv-{layer}-v-after-{pos}"), &v, &shape);
                    arrays += 2;
                }
            }
        }
        times.push(json!({"step":i,"stage_a_embed_export_ms":stage_a_ms,"stage_b_head_export_ms":stage_b_head_ms}));
        ctx.clear_cache().unwrap();
    }
    for (worker, session) in [(&a, &sa), (&b, &sb)] {
        for (layer, shape, k, v) in worker.kv_snapshot(session).unwrap() {
            let shape: Vec<u32> = shape.iter().map(|d| *d as u32).collect();
            check(&fixture, &format!("kv-{layer}-k.npy"), &k, &shape);
            check(&fixture, &format!("kv-{layer}-v.npy"), &v, &shape);
            arrays += 2;
        }
    }
    assert_eq!(arrays, fixture["arrays"].as_object().unwrap().len());
    a.reset(&mut sa).unwrap();
    assert_eq!(sa.position(), 0);
    assert_eq!(sa.state_generation(), 1);
    assert_eq!(
        a.prefill(&mut sa, step(&ba, 0, Input::Tokens(vec![1])))
            .unwrap_err()
            .code,
        ErrorCode::Binding
    );
    a.cancel(&mut sa).unwrap();
    assert_eq!(
        a.prefill(&mut sa, step(&ba, 0, Input::Tokens(vec![1])))
            .unwrap_err()
            .code,
        ErrorCode::Cancelled
    );
    a.release_session(sa).unwrap();
    b.release_session(sb).unwrap();
    let mut foreign = ba.clone();
    foreign.load_generation = 2;
    assert_eq!(
        a.new_session(foreign, cap()).err().unwrap().code,
        ErrorCode::Binding
    );
    let fresh = a.new_session(ba, cap()).unwrap();
    a.release_session(fresh).unwrap();
    a.close().unwrap();
    b.close().unwrap();
    assert_eq!(a.synchronize().unwrap_err().code, ErrorCode::Closed);
    ctx.healthy().unwrap();
    assert_eq!(
        ctx.memory_stats().unwrap().0,
        0,
        "no native tensor allocation after session/stage close"
    );
    println!(
        "L8_PARITY {}",
        json!({"arrays":arrays,"greedy_selections":64,"prefill_tokens":35,"decode_calls":63,"prepare_both_ms":load_ms,"mlx_after_stage_a":after_a,"mlx_after_stage_b":after_b,"mlx_after_close":ctx.memory_stats().unwrap(),"loaded_a_bytes":a.loaded_bytes(),"loaded_b_bytes":b.loaded_bytes(),"times":times})
    );
    assert_eq!(
        Rc::clone(&ctx).close().unwrap_err().code,
        ErrorCode::Cleanup
    );
    drop(a);
    drop(b);
    drop(backend);
    ctx.close().unwrap();
}
