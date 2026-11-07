// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "metal")]
use backend_mlx::Context;
use model::{Tensor, Weight, moe::MoeTensor};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use stage_contract::ErrorCode;
use std::{
    collections::BTreeMap,
    io::{Read, Seek, SeekFrom},
    path::PathBuf,
    time::{Duration, Instant},
};

fn check_payload(expected: &Value, shape: &[i32], words: impl Iterator<Item = u32>) {
    let mut hash = Sha256::new();
    let mut count = 0usize;
    for word in words {
        hash.update(word.to_le_bytes());
        count += 1;
    }
    assert_eq!(expected["shape"], json!(shape));
    assert_eq!(expected["payload_bytes"], json!(count * 4));
    assert_eq!(
        expected["payload_sha256"],
        json!(format!("{:x}", hash.finalize()))
    );
}
macro_rules! check_float {
    ($e:expr, $a:expr) => {{
        assert_eq!($e["dtype"], "float32");
        check_payload(
            $e,
            &$a.shape(),
            $a.floats().unwrap().into_iter().map(f32::to_bits),
        );
    }};
}
fn component(profile: &str) {
    let fixture: Value = serde_json::from_str(match profile {
        "f32" => include_str!("fixtures/moe-component-f32.json"),
        "packed" => include_str!("fixtures/moe-component-packed.json"),
        _ => unreachable!(),
    })
    .unwrap();
    let started = Instant::now();
    // Reuse the registry's consented fixture wall, not a local timeout literal.
    let wall = budgets::BudgetId::MemberChosenWall
        .span(budgets::Entries::ZERO)
        .unwrap();
    let quiet = || {
        assert!(
            started.elapsed() < Duration::from_millis(wall.millis()),
            "bounded component gate"
        );
        let demo = PathBuf::from(std::env::var("HOME").unwrap()).join("mycelium-demo-run");
        if demo.exists() {
            for entry in std::fs::read_dir(demo).unwrap() {
                assert!(
                    !entry
                        .unwrap()
                        .file_name()
                        .to_string_lossy()
                        .ends_with(".busy"),
                    "demo hold"
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
    };
    quiet();
    assert_eq!(
        fixture["scope"],
        "first-layer-moe-block-only-not-stage-or-model-acceptance"
    );
    assert_eq!(
        fixture["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let root = PathBuf::from(
        std::env::var("MYCELIUM_MOE_CHECKPOINT")
            .expect("cached MoE snapshot required; never download"),
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
    let ctx = Context::new(6 * 1024 * 1024 * 1024, 128 * 1024 * 1024).unwrap();
    let mut weights = BTreeMap::new();
    for (shard, identity) in fixture["checkpoint"]["shards"].as_object().unwrap() {
        quiet();
        let path = root.join(shard);
        let mut file = std::fs::File::open(&path).unwrap();
        assert_eq!(
            identity["file_bytes"],
            json!(file.metadata().unwrap().len())
        );
        let mut size = [0; 8];
        file.read_exact(&mut size).unwrap();
        let header_len = u64::from_le_bytes(size);
        assert!(header_len <= 1024 * 1024);
        let mut bytes = vec![0; header_len as usize];
        file.read_exact(&mut bytes).unwrap();
        assert_eq!(
            identity["header_sha256"],
            json!(format!("{:x}", Sha256::digest(&bytes)))
        );
        let header: Value = serde_json::from_slice(&bytes).unwrap();
        let mut wanted = BTreeMap::new();
        for (name, info) in fixture["weights"].as_object().unwrap() {
            if info["shard"] != *shard {
                continue;
            }
            let meta = &header[name];
            assert_eq!(meta["shape"], info["shape"]);
            assert_eq!(meta["dtype"], info["source_dtype"]);
            let begin = meta["data_offsets"][0].as_u64().unwrap();
            let end = meta["data_offsets"][1].as_u64().unwrap();
            assert_eq!(info["source_bytes"], json!(end - begin));
            file.seek(SeekFrom::Start(8 + header_len + begin)).unwrap();
            let mut remaining = end - begin;
            let mut hash = Sha256::new();
            let mut buffer = vec![0; 1024 * 1024];
            while remaining > 0 {
                let count = remaining.min(buffer.len() as u64) as usize;
                file.read_exact(&mut buffer[..count]).unwrap();
                hash.update(&buffer[..count]);
                remaining -= count as u64;
            }
            assert_eq!(
                info["source_sha256"],
                json!(format!("{:x}", hash.finalize()))
            );
            wanted.insert(
                name.clone(),
                serde_json::from_value(info["shape"].clone()).unwrap(),
            );
        }
        if profile == "f32" {
            weights.extend(
                ctx.load(path.to_str().unwrap(), &wanted)
                    .unwrap()
                    .into_iter()
                    .map(|(k, v)| (k, Weight::Float(v))),
            );
        } else {
            weights.extend(ctx.load_packed(path.to_str().unwrap(), &wanted).unwrap());
        }
    }
    assert_eq!(weights.len(), 7);
    for (name, weight) in &weights {
        let expected = &fixture["weights"][name]["resident"];
        match weight {
            Weight::Float(a) => {
                assert_eq!(expected.as_object().unwrap().len(), 1);
                check_float!(&expected["float"], a);
            }
            Weight::Packed {
                codes,
                scales,
                group_scales,
                group_biases,
            } => {
                assert_eq!(expected.as_object().unwrap().len(), 4);
                assert_eq!(expected["packed"]["dtype"], "uint32");
                check_payload(
                    &expected["packed"],
                    &codes.shape(),
                    codes.words().unwrap().into_iter(),
                );
                for (key, a) in [
                    ("scales", scales),
                    ("group_scales", group_scales),
                    ("group_biases", group_biases),
                ] {
                    check_float!(&expected[key], a);
                }
            }
        }
    }
    eprintln!(
        "moe-component profile={profile} verified_load_s={:.6}",
        started.elapsed().as_secs_f64()
    );
    let mut checks = 0;
    for rows in [1, 2, 8, 33, 65] {
        quiet();
        let input: Vec<f32> = (0..rows * 2048)
            .map(|i| ((i % 97 - 48) as f32) / 64.)
            .collect();
        let x = ctx.floats(&input, &[1, rows, 2048]).unwrap();
        let call = Instant::now();
        let result = model::moe::qwen36_first_block(&x, &weights).unwrap();
        result.output.eval().unwrap();
        eprintln!(
            "moe-component profile={profile} rows={rows} materialize_ms={:.6}",
            call.elapsed().as_secs_f64() * 1000.
        );
        for (key, a) in [
            ("input", &x),
            ("probabilities", &result.probabilities),
            ("scores", &result.scores),
            ("output", &result.output),
        ] {
            eprintln!("CHECK {rows}-{key}");
            check_float!(&fixture["arrays"][format!("{rows}-{key}")], a);
            checks += 1;
        }
        check_payload(
            &fixture["arrays"][format!("{rows}-indices")],
            &result.indices.shape(),
            result.indices.words().unwrap().into_iter(),
        );
        checks += 1;
    }
    assert_eq!(checks, fixture["arrays"].as_object().unwrap().len());
    ctx.synchronize().unwrap();
    eprintln!(
        "moe-component profile={profile} total_s={:.6}",
        started.elapsed().as_secs_f64()
    );
}
#[test]
fn qwen36_first_moe_block_f32() {
    component("f32");
}
#[test]
fn qwen36_first_moe_block_packed() {
    component("packed");
}

#[test]
fn moe_routing_ties_shapes_and_expert_bounds() {
    let ctx = Context::new(64 * 1024 * 1024, 0).unwrap();
    let p = ctx
        .floats(&[0.25, 0.25, 0.25, 0.25, 0.1, 0.4, 0.4, 0.1], &[2, 4])
        .unwrap();
    let (scores, ids) = p.route_top_k(2).unwrap();
    assert_eq!(ids.words().unwrap(), vec![0, 1, 1, 2]);
    assert_eq!(scores.floats().unwrap(), vec![0.5; 4]);
    assert_eq!(
        p.route_top_k(0).err().unwrap().code,
        ErrorCode::MalformedTensor
    );
    assert_eq!(
        p.route_top_k(5).err().unwrap().code,
        ErrorCode::MalformedTensor
    );
    let unequal = ctx.floats(&[0.125, 0.625, 0.125, 0.125], &[1, 4]).unwrap();
    assert_eq!(
        unequal.route_top_k(3).unwrap().1.words().unwrap(),
        vec![1, 0, 2]
    );
    let scalar = p.scalar(1.).unwrap();
    assert_eq!(
        scalar.route_top_k(1).err().unwrap().code,
        ErrorCode::MalformedTensor
    );
    let weights = Weight::Float(ctx.floats(&[1.; 8], &[2, 2, 2]).unwrap());
    let x = ctx.floats(&[1.; 4], &[2, 1, 2]).unwrap();
    let invalid = ctx.words(&[0, 2], &[2, 1]).unwrap();
    assert_eq!(
        x.expert_linear(&weights, &invalid).err().unwrap().code,
        ErrorCode::MalformedTensor
    );
    let valid = ctx.words(&[0, 1], &[2, 1]).unwrap();
    assert_eq!(
        x.expert_linear(&weights, &valid).unwrap().floats().unwrap(),
        vec![2.; 4]
    );
    ctx.synchronize().unwrap();
}

#[test]
fn expert_loader_packs_3d_but_never_routing_weights() {
    use std::{collections::BTreeMap, io::Write};
    let ctx = Context::new(64 * 1024 * 1024, 0).unwrap();
    let expert = "model.language_model.layers.0.mlp.experts.gate_up_proj";
    let router = "model.language_model.layers.0.mlp.gate.weight";
    let shared_router = "model.language_model.layers.0.mlp.shared_expert_gate.weight";
    let wanted = BTreeMap::from([
        (expert.to_owned(), vec![2, 2, 128]),
        (router.to_owned(), vec![2, 128]),
        (shared_router.to_owned(), vec![1, 128]),
    ]);
    let mut header = serde_json::Map::new();
    let mut payload = Vec::new();
    for (name, shape) in &wanted {
        let start = payload.len();
        let count: i32 = shape.iter().product();
        for i in 0..count {
            payload.extend_from_slice(&((i % 128) as f32).to_le_bytes());
        }
        header.insert(
            name.clone(),
            serde_json::json!({"dtype":"F32", "shape":shape, "data_offsets":[start,payload.len()]}),
        );
    }
    let bytes = serde_json::to_vec(&header).unwrap();
    let path = std::env::temp_dir().join(format!(
        "l8-expert-loader-{}.safetensors",
        std::process::id()
    ));
    let mut file = std::fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&path)
        .unwrap();
    file.write_all(&(bytes.len() as u64).to_le_bytes()).unwrap();
    file.write_all(&bytes).unwrap();
    file.write_all(&payload).unwrap();
    drop(file);
    let weights = ctx.load_packed(path.to_str().unwrap(), &wanted).unwrap();
    std::fs::remove_file(path).unwrap();
    assert!(matches!(weights[expert], Weight::Packed { .. }));
    assert!(matches!(weights[router], Weight::Float(_)));
    assert!(matches!(weights[shared_router], Weight::Float(_)));
    assert_eq!(weights[expert].shape(), vec![2, 2, 128]);
    ctx.synchronize().unwrap();
}
