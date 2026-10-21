// SPDX-License-Identifier: AGPL-3.0-only
//! Parity of the Qwen2 stage against the NumPy serving path
//! (`numpy_runtime._qwen2_block_with_kv`, `quantize_qwen2_numpy_tensor`), on
//! fixtures from `tests/fixtures/generate_qwen2_fixtures.py`.
//!
//! - Always: a tiny synthetic Qwen2 (exact quantizer, tolerance vs NumPy,
//!   host golden digest) and NumPy's float32 pairwise sum.
//! - With `MYCELIUM_QWEN05_SNAPSHOT` set to a Qwen2.5-0.5B-Instruct snapshot:
//!   the real layer-12 weights (quantizer bit-identical on all seven
//!   matrices, tolerance vs NumPy, host golden digest). Without it those
//!   tests print a skip line and pass.
//! - `cargo test --release --test qwen2_parity -- --ignored --nocapture`:
//!   single-thread timing of one real layer.
//! - On a phone: build with `--target aarch64-linux-android --no-run`, push the
//!   binary and `tests/fixtures`, set `MYCELIUM_MOBILE_FIXTURES`; the golden
//!   digests must then match the host's (plan R1a).

use std::path::PathBuf;
use std::time::Instant;

use base64::Engine;
use mycelium_mobile_core::qwen2::{MATRIX_SUFFIXES, Qwen2Config, Qwen2Stage, SafetensorsBytes, pairwise_sum};
use mycelium_mobile_core::quant::Int8Rowwise;
use mycelium_mobile_core::safetensors::SafetensorsFile;
use serde_json::Value;
use sha2::{Digest, Sha256};

/// SHA-256 of the host build's f32 LE output for the synthetic fixture (both
/// layers). The phone build must reproduce it bit for bit (plan R1a).
const SYNTHETIC_GOLDEN: &str = "sha256:6b8cd240399d0b831c6eca3be09f6b34f1caea4b6e412b9c554cb943c187c5f2";
/// Same for the real Qwen2.5-0.5B layer-12 fixture.
const REAL_LAYER12_GOLDEN: &str = "sha256:0e6115d549d4e8f2f83529b937c595ea435e1151302bde3b453296f14694d3be";

/// Fixtures live in `tests/fixtures`; `MYCELIUM_MOBILE_FIXTURES` points a
/// cross-compiled test binary at a copy on the device (`adb push`).
fn fixture(name: &str) -> Value {
    let dir = std::env::var("MYCELIUM_MOBILE_FIXTURES").unwrap_or_else(|_| format!("{}/tests/fixtures", env!("CARGO_MANIFEST_DIR")));
    let path = format!("{dir}/{name}");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn b64(v: &Value) -> Vec<u8> {
    base64::engine::general_purpose::STANDARD.decode(v.as_str().unwrap()).unwrap()
}

fn f32s(v: &Value) -> Vec<f32> {
    b64(v).chunks_exact(4).map(|c| f32::from_le_bytes(c.try_into().unwrap())).collect()
}

fn sha(bytes: &[u8]) -> String {
    format!("sha256:{}", hex::encode(Sha256::digest(bytes)))
}

fn f32_bytes(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
}

fn i8_bytes(v: &[i8]) -> Vec<u8> {
    v.iter().map(|x| *x as u8).collect()
}

/// (max abs diff, max |reference|).
fn compare(ours: &[f32], reference: &[f32]) -> (f32, f32) {
    assert_eq!(ours.len(), reference.len());
    let diff = ours.iter().zip(reference).map(|(a, b)| (a - b).abs()).fold(0.0f32, f32::max);
    let scale = reference.iter().map(|v| v.abs()).fold(0.0f32, f32::max);
    (diff, scale)
}

fn assert_quantizer(stage: &Qwen2Stage, expected: &Value, offset: usize) {
    let layer = &stage.layers()[offset];
    for suffix in MATRIX_SUFFIXES {
        let m = layer.matrix(suffix).unwrap();
        let e = &expected[suffix];
        assert_eq!(serde_json::json!([m.rows, m.cols]), e["shape"], "{suffix}");
        assert_eq!(sha(&i8_bytes(&m.values)), e["values_sha256"].as_str().unwrap(), "{suffix} values");
        assert_eq!(sha(&f32_bytes(&m.scales)), e["scales_sha256"].as_str().unwrap(), "{suffix} scales");
    }
}

#[test]
fn numpy_float32_sum_is_reproduced_bit_for_bit() {
    let f = fixture("qwen2_synthetic.json");
    for case in f["sum_cases"].as_array().unwrap() {
        let values = f32s(&case["values_b64"]);
        assert_eq!(pairwise_sum(&values).to_bits() as u64, case["sum_bits"].as_u64().unwrap(), "n={}", values.len());
    }
}

#[test]
fn quantizer_matches_numpy_bit_for_bit_on_edge_rows() {
    let e = &fixture("qwen2_synthetic.json")["quantizer_edge"];
    let rows = f32s(&e["rows_f32_b64"]);
    let (r, c) = (e["shape"][0].as_u64().unwrap() as usize, e["shape"][1].as_u64().unwrap() as usize);
    let q = Int8Rowwise::from_f32(r, c, &rows).unwrap();
    let values: Vec<i64> = e["values"].as_array().unwrap().iter().flat_map(|row| row.as_array().unwrap().iter().map(|v| v.as_i64().unwrap())).collect();
    assert_eq!(q.values.iter().map(|v| i64::from(*v)).collect::<Vec<_>>(), values);
    let scales: Vec<u64> = e["scales_bits"].as_array().unwrap().iter().map(|v| v.as_u64().unwrap()).collect();
    assert_eq!(q.scales.iter().map(|s| u64::from(s.to_bits())).collect::<Vec<_>>(), scales);
}

fn synthetic_stage(f: &Value) -> Qwen2Stage {
    let blob = b64(&f["safetensors_b64"]);
    assert_eq!(sha(&blob), f["safetensors_sha256"].as_str().unwrap());
    let config = Qwen2Config::from_hf_config(&f["config"]).unwrap();
    Qwen2Stage::load(config, 0, 2, &mut SafetensorsBytes::new(&blob).unwrap()).unwrap()
}

#[test]
fn synthetic_stage_quantizes_like_numpy_and_tracks_it() {
    let f = fixture("qwen2_synthetic.json");
    let stage = synthetic_stage(&f);
    assert_quantizer(&stage, &f["quantizer"]["layer0"], 0);
    assert_quantizer(&stage, &f["quantizer"]["layer1"], 1);
    let seq = f["seq"].as_u64().unwrap() as usize;
    let mut x = f32s(&f["input_b64"]);
    for (offset, reference) in f["numpy_layer_outputs_b64"].as_array().unwrap().iter().enumerate() {
        // Each layer from the NumPy input of that layer, so errors do not compound.
        let ours = stage.forward_layer(offset, seq, &x).unwrap();
        let reference = f32s(reference);
        let (diff, scale) = compare(&ours, &reference);
        println!("synthetic layer {offset}: max_abs_diff={diff:e} max_abs_ref={scale} rel={:e}", diff / scale);
        assert!(diff <= 2e-6 * scale, "layer {offset}: {diff} vs {scale}");
        x = reference;
    }
}

#[test]
fn synthetic_stage_is_deterministic_and_matches_the_host_golden() {
    let f = fixture("qwen2_synthetic.json");
    let stage = synthetic_stage(&f);
    let seq = f["seq"].as_u64().unwrap() as usize;
    let input = f32s(&f["input_b64"]);
    let a = stage.forward(seq, &input).unwrap();
    let b = stage.forward(seq, &input).unwrap();
    assert_eq!(f32_bytes(&a), f32_bytes(&b));
    let chained = stage.forward_layer(1, seq, &stage.forward_layer(0, seq, &input).unwrap()).unwrap();
    assert_eq!(f32_bytes(&a), f32_bytes(&chained));
    let reference = f32s(f["numpy_layer_outputs_b64"].as_array().unwrap().last().unwrap());
    let (diff, scale) = compare(&a, &reference);
    println!("synthetic 2-layer stage: max_abs_diff={diff:e} max_abs_ref={scale}");
    assert!(diff <= 4e-6 * scale);
    println!("synthetic golden {}", sha(&f32_bytes(&a)));
    assert_eq!(sha(&f32_bytes(&a)), SYNTHETIC_GOLDEN);
}

#[test]
fn synthetic_stage_fails_closed() {
    let f = fixture("qwen2_synthetic.json");
    let stage = synthetic_stage(&f);
    let seq = f["seq"].as_u64().unwrap() as usize;
    let input = f32s(&f["input_b64"]);
    assert_eq!(stage.forward(seq, &input[1..]).unwrap_err().0, "request_hidden_shape_invalid");
    assert_eq!(stage.forward(0, &[]).unwrap_err().0, "request_hidden_shape_invalid");
    let mut bad = input.clone();
    bad[3] = f32::NAN;
    assert_eq!(stage.forward(seq, &bad).unwrap_err().0, "request_hidden_nonfinite");

    let blob = b64(&f["safetensors_b64"]);
    let config = Qwen2Config::from_hf_config(&f["config"]).unwrap();
    let load = |cfg: Qwen2Config, s: usize, e: usize| Qwen2Stage::load(cfg, s, e, &mut SafetensorsBytes::new(&blob).unwrap()).err().unwrap().0;
    assert_eq!(load(config.clone(), 1, 3), "qwen2_tensor_missing");
    assert_eq!(load(config.clone(), 1, 1), "qwen2_layer_range_invalid");
    let mut wide = config.clone();
    wide.intermediate_size = 256;
    assert_eq!(load(wide, 0, 1), "qwen2_tensor_shape_invalid");
    let mut tampered = blob.clone();
    tampered[9] ^= 0x01;
    assert!(SafetensorsBytes::new(&tampered).is_err());
}

// ---- real Qwen2.5-0.5B-Instruct weights (opt-in) ---------------------------

fn snapshot() -> Option<PathBuf> {
    match std::env::var_os("MYCELIUM_QWEN05_SNAPSHOT") {
        Some(p) => Some(PathBuf::from(p)),
        None => {
            println!("SKIP: MYCELIUM_QWEN05_SNAPSHOT not set");
            None
        }
    }
}

fn real_stage(dir: &std::path::Path, f: &Value) -> (Qwen2Stage, f64) {
    let config: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("config.json")).unwrap()).unwrap();
    let config = Qwen2Config::from_hf_config(&config).unwrap();
    let layer = f["layer"].as_u64().unwrap() as usize;
    let mut file = SafetensorsFile::open(&dir.join("model.safetensors")).unwrap();
    let started = Instant::now();
    let stage = Qwen2Stage::load(config, layer, layer + 1, &mut file).unwrap();
    (stage, started.elapsed().as_secs_f64() * 1e3)
}

#[test]
fn real_qwen05_layer_quantizes_like_numpy_and_tracks_it() {
    let Some(dir) = snapshot() else { return };
    let f = fixture("qwen05_layer12.json");
    assert_eq!(dir.file_name().unwrap().to_str().unwrap(), f["resolved_commit"].as_str().unwrap());
    let (stage, load_ms) = real_stage(&dir, &f);
    println!("real layer {} loaded+quantized in {load_ms:.0} ms, resident {} bytes", f["layer"], stage.resident_bytes());
    assert_quantizer(&stage, &f["quantizer"], 0);

    let seq = f["seq"].as_u64().unwrap() as usize;
    let input = f32s(&f["input_b64"]);
    let ours = stage.forward(seq, &input).unwrap();
    let reference = f32s(&f["numpy_output_b64"]);
    let (diff, scale) = compare(&ours, &reference);
    // Error relative to the residual stream excluding the massive-activation channels.
    let delta_ref: Vec<f32> = reference.iter().zip(&input).map(|(r, i)| r - i).collect();
    let delta_scale = delta_ref.iter().map(|v| v.abs()).fold(0.0f32, f32::max);
    println!(
        "real layer {}: seq={seq} max_abs_diff={diff:e} max_abs_ref={scale} rel={:e} max_abs_layer_delta={delta_scale}",
        f["layer"],
        diff / scale
    );
    // Plan bound 1e-4 x max|h|; frozen tighter from the observed 6.7e-6
    // (Apple M-series host, NumPy 2.5.1 reference) so a kernel regression shows.
    assert!(diff <= 1e-4 * scale, "{diff} vs {scale}");
    assert!(diff <= 3e-5, "{diff}");
    println!("real golden {}", sha(&f32_bytes(&ours)));
    assert_eq!(sha(&f32_bytes(&ours)), REAL_LAYER12_GOLDEN);
}

#[test]
#[ignore = "timing; run with --release --ignored"]
fn real_qwen05_layer_single_thread_timing() {
    let Some(dir) = snapshot() else { return };
    let f = fixture("qwen05_layer12.json");
    let (stage, load_ms) = real_stage(&dir, &f);
    let hidden = stage.config().hidden_size;
    let base = f32s(&f["input_b64"]);
    let rows = base.len() / hidden;
    for seq in [1usize, 8, 16, 32, 64] {
        let input: Vec<f32> = (0..seq).flat_map(|t| base[(t % rows) * hidden..(t % rows + 1) * hidden].iter().copied()).collect();
        let _ = stage.forward(seq, &input).unwrap();
        let reps = if seq <= 16 { 10 } else { 4 };
        let started = Instant::now();
        for _ in 0..reps {
            std::hint::black_box(stage.forward(seq, &input).unwrap());
        }
        let ms = started.elapsed().as_secs_f64() * 1e3 / reps as f64;
        println!("TIMING layer=1 seq={seq} ms_per_forward={ms:.2} ms_per_token={:.3} load_ms={load_ms:.0}", ms / seq as f64);
    }
}
