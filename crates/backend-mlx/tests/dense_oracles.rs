// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "metal")]
use backend_mlx::{
    Context, MlxBackend, Session, Worker,
    admission::{admit, qwen2_packed_descriptor, qwen3_descriptor, qwen35_descriptor},
    payload_id,
};
use model::{DenseConfig, Qwen2, Weight};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use stage_contract::*;
use std::{collections::BTreeSet, path::PathBuf, rc::Rc, time::Instant};
fn quiet() {
    let root = PathBuf::from(std::env::var("HOME").unwrap()).join("mycelium-demo-run");
    if root.exists() {
        for e in std::fs::read_dir(root).unwrap() {
            assert!(
                !e.unwrap().file_name().to_string_lossy().ends_with(".busy"),
                "host hold"
            );
        }
    }
    assert_eq!(
        std::process::Command::new("pgrep")
            .args(["-f", "[P]ARENT-cert-.*-when-quiet"])
            .status()
            .unwrap()
            .code(),
        Some(1)
    );
}
fn cap() -> Limits {
    Limits {
        weight_bytes: 4_000_000_000,
        state_bytes: 32_000_000,
        scratch_bytes: 2_048_000_000,
        max_tokens: 128,
    }
}
fn binding(d: StageDescriptor) -> Binding {
    Binding {
        request: [3; 32],
        assignment: [4; 32],
        load_generation: 1,
        descriptor: d,
    }
}
fn step(b: &Binding, s: &Session, input: Input) -> Step {
    Step {
        binding: b.clone(),
        state_generation: s.state_generation(),
        position: s.position(),
        payload: payload_id(&input),
        input,
    }
}
fn check(f: &Value, seen: &mut BTreeSet<String>, name: &str, bytes: &[u8], shape: &[u32]) {
    let e = &f["arrays"][name];
    assert!(e.is_object(), "missing {name}");
    assert_eq!(e["shape"], json!(shape), "shape {name}");
    assert_eq!(e["payload_bytes"], json!(bytes.len()), "bytes {name}");
    assert_eq!(
        e["payload_sha256"],
        json!(format!("{:x}", Sha256::digest(bytes))),
        "bit exact {name}"
    );
    seen.insert(name.into());
}
fn check_kv(
    f: &Value,
    seen: &mut BTreeSet<String>,
    w: &Worker,
    s: &Session,
    suffix: &str,
    replay: bool,
) {
    for snapshot in w.state_snapshot(s).unwrap() {
        let layer = snapshot.layer;
        let kind = snapshot.kind;
        let shape: Vec<u32> = snapshot.shape.iter().map(|v| *v as u32).collect();
        let name = if replay {
            format!("replay-kv-{layer}-{kind}")
        } else {
            format!("kv-{layer}-{kind}-{suffix}")
        };
        check(f, seen, &name, &snapshot.bytes, &shape);
    }
}
#[test]
fn qwen2_packed_own_oracle_weights_layers_decode_replay_unsplit_refusals() {
    qualify(
        include_str!("fixtures/qwen2-packed.json"),
        "MYCELIUM_QWEN2_CHECKPOINT",
    );
}
#[test]
fn qwen3_packed_own_oracle_weights_layers_decode_replay_unsplit_refusals() {
    qualify(
        include_str!("fixtures/qwen3-packed.json"),
        "MYCELIUM_QWEN3_CHECKPOINT",
    );
}
#[test]
fn qwen3_f32_own_oracle_weights_layers_decode_replay_unsplit_refusals() {
    qualify(
        include_str!("fixtures/qwen3-f32.json"),
        "MYCELIUM_QWEN3_CHECKPOINT",
    );
}
#[test]
fn qwen35_packed_own_oracle_all_hybrid_states_decode_replay_refusals() {
    qualify(
        include_str!("fixtures/qwen35-packed.json"),
        "MYCELIUM_QWEN35_CHECKPOINT",
    );
}
#[test]
fn qwen35_f32_own_oracle_conv_recurrence_attention_decode_replay_unsplit_refusals() {
    qualify(
        include_str!("fixtures/qwen35-f32.json"),
        "MYCELIUM_QWEN35_CHECKPOINT",
    );
}

fn qualify(fixture: &str, env: &str) {
    quiet();
    let f: Value = serde_json::from_str(fixture).unwrap();
    assert_eq!(
        f["oracle_commit"],
        "941765e882ed3eea9a5576cc53e525c4caa20300"
    );
    let model =
        PathBuf::from(std::env::var(env).expect("BLOCKED: cached pinned checkpoint required"));
    let artifact = Artifacts {
        checkpoint: model.clone(),
    };
    // Aggregate admission reserves both workers' complete logical worst-case
    // scratch, even though this fixture executes them serially. The old 6 GiB
    // grant correctly refuses hybrid FP32; this synthetic fixture explicitly
    // grants 8 GiB for that cell, without changing its 128-token limits/workload.
    let native_gib = if f["architecture"] == "qwen35" && f["profile"] == "f32" {
        8
    } else {
        6
    };
    let ctx = Context::new(native_gib * 1024 * 1024 * 1024, 256 * 1024 * 1024).unwrap();
    let mut backend = MlxBackend::new(Rc::clone(&ctx));
    let split = f["split"].as_u64().unwrap() as u32;
    let layers = f["layers"].as_u64().unwrap() as u32;
    let packed = f["profile"] == "packed";
    let profile = if packed {
        NumericsProfile::MlxPackedInt8
    } else {
        NumericsProfile::MlxExplicitF32
    };
    let desc = |range, embedding, head| {
        if f["architecture"] == "qwen35" {
            qwen35_descriptor(range, embedding, head, profile).unwrap()
        } else if f["architecture"] == "qwen3" {
            qwen3_descriptor(range, embedding, head, profile).unwrap()
        } else {
            qwen2_packed_descriptor(range, embedding, head).unwrap()
        }
    };
    let da = desc(0..split, true, false);
    let cfg = DenseConfig::for_descriptor(&da).unwrap();
    let db = desc(split..layers, false, true);
    for d in [&da, &db] {
        for field in 0..4 {
            let mut limits = cap();
            match field {
                0 => limits.weight_bytes = admit(d, cap()).unwrap() - 1,
                1 => limits.state_bytes = 1,
                2 => limits.scratch_bytes = 1,
                _ => limits.max_tokens = 0,
            }
            assert_eq!(
                backend
                    .prepare(
                        d.clone(),
                        &Artifacts {
                            checkpoint: "must-not-open".into()
                        },
                        limits,
                        1
                    )
                    .err()
                    .unwrap()
                    .code,
                ErrorCode::ResourceCap
            );
        }
    }
    let load = Instant::now();
    let mut a = backend.prepare(da.clone(), &artifact, cap(), 1).unwrap();
    let mem_a = ctx.memory_stats().unwrap();
    let mut b = backend.prepare(db.clone(), &artifact, cap(), 1).unwrap();
    let mem_b = ctx.memory_stats().unwrap();
    let load_ms = load.elapsed().as_secs_f64() * 1000.;
    // Metal allocator.cpp:124-130 rounds large allocations to VM pages.
    // common/buffer_cache.h:30-35 can reuse a buffer strictly less than two
    // additional pages larger than the aligned request. Apply that independent
    // per-component ceiling to the sealed image (16KiB host VM pages), not a
    // measured fudge factor or permission for a full extra resident image.
    let allocated_bound = |w: &Worker| -> u64 {
        w.loaded_names()
            .iter()
            .map(|n| {
                f["weights"][*n]
                    .as_object()
                    .unwrap()
                    .values()
                    .map(|p| (p["payload_bytes"].as_u64().unwrap().div_ceil(16384) + 2) * 16384 - 1)
                    .sum::<u64>()
            })
            .sum()
    };
    assert!(allocated_bound(&a) < 2 * a.loaded_bytes());
    assert!(allocated_bound(&b) < 2 * b.loaded_bytes());
    assert!((a.loaded_bytes()..=allocated_bound(&a)).contains(&(mem_a.0 as u64)));
    assert!(
        (a.loaded_bytes() + b.loaded_bytes()..=allocated_bound(&a) + allocated_bound(&b))
            .contains(&(mem_b.0 as u64))
    );
    let mut weight_names = BTreeSet::new();
    for w in [&a, &b] {
        for (name, value) in w.weight_digests().unwrap().as_object().unwrap() {
            assert_eq!(*value, f["weights"][name], "resident packed image {name}");
            weight_names.insert(name.clone());
        }
    }
    if f["weights"].get("lm_head.weight").is_some() {
        // This checkpoint physically duplicates the tied embedding image. The
        // pinned config/oracle selects embedding for head; do not load both.
        assert_eq!(
            f["weights"]["lm_head.weight"],
            f["weights"]["model.embed_tokens.weight"]
        );
        weight_names.insert("lm_head.weight".into());
    }
    assert_eq!(weight_names.len(), f["weights"].as_object().unwrap().len());
    let edge = &f["quantizer_edges"];
    let input: Vec<f32> = edge["input"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|r| {
            r.as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_f64().unwrap() as f32)
        })
        .collect();
    let (codes, scales) = model::quant::pack_rows(&input, 128).unwrap();
    let signed: Vec<i32> = codes
        .iter()
        .flat_map(|w| w.to_le_bytes())
        .map(|v| (v ^ 128) as i8 as i32)
        .collect();
    let expected: Vec<i32> = edge["codes"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|r| {
            r.as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_i64().unwrap() as i32)
        })
        .collect();
    assert_eq!(signed, expected);
    assert_eq!(
        json!(scales.iter().map(|v| v.to_bits()).collect::<Vec<_>>()),
        edge["scale_bits"]
    );
    let ba = binding(da);
    let bb = binding(db);
    let mut sa = a.new_session(ba.clone(), cap()).unwrap();
    let mut sb = b.new_session(bb.clone(), cap()).unwrap();
    assert_eq!(a.close().unwrap_err().code, ErrorCode::Cleanup);
    for ids in [vec![cfg.vocab as u32], vec![1; 129]] {
        let bad = step(&ba, &sa, Input::Tokens(ids));
        assert!(a.prefill(&mut sa, bad).is_err());
    }
    assert_eq!(sa.position(), 0);
    assert!(a.state_snapshot(&sa).unwrap().is_empty());
    if cfg.hybrid {
        assert_eq!(
            a.kv_snapshot(&sa).err().unwrap().code,
            ErrorCode::Unsupported
        );
        assert_eq!(a.capabilities().state, StateKind::Hybrid);
    } else {
        assert_eq!(a.capabilities().state, StateKind::AttentionKv);
    }
    assert!(!a.capabilities().rewind);
    let prompt: Vec<u32> = f["workload"]["prompt_ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_u64().unwrap() as u32)
        .collect();
    let mut ids = prompt.clone();
    let mut seen = BTreeSet::new();
    let mut times = Vec::new();
    for i in 0..64 {
        quiet();
        assert_eq!(json!(ids), f["feeds"][i]);
        let st = step(&ba, &sa, Input::Tokens(ids));
        let duplicate = st.clone();
        let t = Instant::now();
        let oa = if i == 0 {
            a.prefill(&mut sa, st)
        } else {
            a.decode(&mut sa, st)
        }
        .unwrap();
        let ams = t.elapsed().as_secs_f64() * 1000.;
        check(
            &f,
            &mut seen,
            &format!("stage_a-{i}"),
            &oa.hidden.bytes,
            &oa.hidden.shape,
        );
        let st = step(&bb, &sb, Input::Hidden(oa.hidden));
        let t = Instant::now();
        let ob = if i == 0 {
            b.prefill(&mut sb, st)
        } else {
            b.decode(&mut sb, st)
        }
        .unwrap();
        let bms = t.elapsed().as_secs_f64() * 1000.;
        check(
            &f,
            &mut seen,
            &format!("stage_b-{i}"),
            &ob.hidden.bytes,
            &ob.hidden.shape,
        );
        let logits = ob.logits.unwrap();
        check(
            &f,
            &mut seen,
            &format!("logits-{i}"),
            &logits.bytes,
            &[cfg.vocab as u32],
        );
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
        assert_eq!(json!(token), f["tokens"][i]);
        ids = vec![token];
        check_kv(&f, &mut seen, &a, &sa, &i.to_string(), false);
        check_kv(&f, &mut seen, &b, &sb, &i.to_string(), false);
        assert_eq!(
            a.decode(&mut sa, duplicate).unwrap_err().code,
            ErrorCode::Position
        );
        if i == 0 {
            let mut rewind = step(&ba, &sa, Input::Tokens(vec![1]));
            rewind.position = 1;
            assert_eq!(
                a.decode(&mut sa, rewind).unwrap_err().code,
                ErrorCode::Position
            );
            let bad = Activation {
                shape: [2, 1, cfg.hidden as u32],
                dtype: DType::F32,
                order: ByteOrder::Little,
                layout: Layout::BatchSequenceHidden,
                bytes: vec![0; 2 * cfg.hidden as usize * 4],
            };
            let bad = step(&bb, &sb, Input::Hidden(bad));
            assert_eq!(
                b.decode(&mut sb, bad).unwrap_err().code,
                ErrorCode::MalformedTensor
            );
        }
        times.push(json!({"step":i,"stage_a_ms":ams,"stage_b_ms":bms}));
        ctx.clear_cache().unwrap();
    }
    a.reset(&mut sa).unwrap();
    b.reset(&mut sb).unwrap();
    let mut stale = step(&ba, &sa, Input::Tokens(prompt.clone()));
    stale.state_generation = 0;
    assert_eq!(
        a.prefill(&mut sa, stale).unwrap_err().code,
        ErrorCode::Binding
    );
    let replay: Vec<u32> = f["replay_ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_u64().unwrap() as u32)
        .collect();
    let st = step(&ba, &sa, Input::Tokens(replay));
    let oa = a.prefill(&mut sa, st).unwrap();
    check(
        &f,
        &mut seen,
        "replay-stage_a",
        &oa.hidden.bytes,
        &oa.hidden.shape,
    );
    let st = step(&bb, &sb, Input::Hidden(oa.hidden));
    let ob = b.prefill(&mut sb, st).unwrap();
    check(
        &f,
        &mut seen,
        "replay-stage_b",
        &ob.hidden.bytes,
        &ob.hidden.shape,
    );
    check(
        &f,
        &mut seen,
        "replay-logits",
        &ob.logits.unwrap().bytes,
        &[cfg.vocab as u32],
    );
    check_kv(&f, &mut seen, &a, &sa, "", true);
    check_kv(&f, &mut seen, &b, &sb, "", true);
    a.cancel(&mut sa).unwrap();
    let st = step(&ba, &sa, Input::Tokens(vec![1]));
    assert_eq!(
        a.decode(&mut sa, st).unwrap_err().code,
        ErrorCode::Cancelled
    );
    a.release_session(sa).unwrap();
    b.release_session(sb).unwrap();
    a.close().unwrap();
    b.close().unwrap();
    drop(a);
    drop(b);
    assert_eq!(ctx.memory_stats().unwrap().0, 0);
    quiet();
    // Unsplit is exercised through the same real stage factory, not inferred.
    let d = desc(0..layers, true, true);
    let bd = binding(d.clone());
    let mut whole = backend.prepare(d, &artifact, cap(), 1).unwrap();
    let mut s = whole.new_session(bd.clone(), cap()).unwrap();
    let st = step(&bd, &s, Input::Tokens(prompt.clone()));
    let o = whole.prefill(&mut s, st).unwrap();
    check(&f, &mut seen, "stage_b-0", &o.hidden.bytes, &o.hidden.shape);
    check(
        &f,
        &mut seen,
        "logits-0",
        &o.logits.unwrap().bytes,
        &[cfg.vocab as u32],
    );
    check_kv(&f, &mut seen, &whole, &s, "0", false);
    whole.release_session(s).unwrap();
    whole.close().unwrap();
    drop(whole);
    // Smallest assigned slice separately checks embedding and first block.
    let d = desc(0..1, true, false);
    let w = if packed {
        ctx.load_packed(model.to_str().unwrap(), &Qwen2::weights(&d).unwrap())
            .unwrap()
    } else {
        ctx.load(model.to_str().unwrap(), &Qwen2::weights(&d).unwrap())
            .unwrap()
            .into_iter()
            .map(|(k, v)| (k, Weight::Float(v)))
            .collect()
    };
    let x = w[cfg.embedding_key()]
        .embedding(&ctx.ids(&prompt).unwrap())
        .unwrap();
    let bytes: Vec<u8> = x
        .floats()
        .unwrap()
        .iter()
        .flat_map(|v| v.to_le_bytes())
        .collect();
    check(
        &f,
        &mut seen,
        "embedding",
        &bytes,
        &[1, prompt.len() as u32, cfg.hidden as u32],
    );
    let (x, kv) = model::forward(&d, x, &w, &vec![None], 0).unwrap();
    let bytes: Vec<u8> = x
        .floats()
        .unwrap()
        .iter()
        .flat_map(|v| v.to_le_bytes())
        .collect();
    check(
        &f,
        &mut seen,
        "layer0",
        &bytes,
        &[1, prompt.len() as u32, cfg.hidden as u32],
    );
    drop(x);
    drop(kv);
    drop(w);
    ctx.clear_cache().unwrap();
    assert_eq!(
        seen.len(),
        f["arrays"].as_object().unwrap().len(),
        "every recorded array compared"
    );
    assert_eq!(ctx.memory_stats().unwrap().0, 0);
    println!(
        "L8_CELL {}",
        json!({"architecture":f["architecture"],"profile":f["profile"],"arrays":seen.len(),"weights":weight_names.len(),"greedy_selections":64,"prepare_ms":load_ms,"mlx_after_a":mem_a,"mlx_after_b":mem_b,"mlx_after_close":ctx.memory_stats().unwrap(),"times":times})
    );
    drop(backend);
    ctx.close().unwrap();
}
