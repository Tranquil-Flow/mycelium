// SPDX-License-Identifier: AGPL-3.0-only
//! Host golden for a v2 stage manifest: run the same Rust stage the phone
//! runs over the deterministic `splitmix64_unit_f32_v1` input and print the
//! golden object `qwen_stage_manifest.py build --golden` takes.
//!
//! ```text
//! cargo run --release --example qwen2_stage_golden -- <snapshot-dir> <start> <end> <seed> <seq>
//! ```

use mycelium_mobile_core::qwen2::{Qwen2Config, Qwen2Stage};
use mycelium_mobile_core::safetensors::SafetensorsFile;
use mycelium_mobile_core::stage_manifest::{GOLDEN_GENERATOR, GOLDEN_PROTOCOL, golden_digest};
use serde_json::{Value, json};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 6 {
        eprintln!("usage: qwen2_stage_golden <snapshot-dir> <start> <end> <seed> <seq>");
        std::process::exit(2);
    }
    let dir = std::path::PathBuf::from(&args[1]);
    let n = |i: usize| args[i].parse::<u64>().expect("integer argument");
    let config: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("config.json")).unwrap()).unwrap();
    let config = Qwen2Config::from_hf_config(&config).unwrap();
    let mut file = SafetensorsFile::open(&dir.join("model.safetensors")).unwrap();
    let stage = Qwen2Stage::load(config, n(2) as usize, n(3) as usize, &mut file).unwrap();
    let digest = golden_digest(&stage, n(4), n(5) as usize).unwrap();
    println!(
        "{}",
        json!({"protocol": GOLDEN_PROTOCOL, "generator": GOLDEN_GENERATOR, "seed": n(4), "seq": n(5), "output_digest": digest})
    );
}
