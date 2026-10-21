// SPDX-License-Identifier: AGPL-3.0-only
//! Host executor for the Qwen2 stage: the same Rust code the phone runs, on
//! the host, so a Python harness (or later the route builder's challenge) can
//! compute a phone stage's output exactly.
//!
//! ```text
//! cargo run --release --example qwen2_stage_exec -- <snapshot-dir> <start> <end>
//! ```
//!
//! Loads layers `[start, end)` from `<snapshot-dir>/model.safetensors` and
//! `config.json`, then serves one JSON request per stdin line:
//! `{"seq": N, "input": "<f32 LE file>", "output": "<f32 LE file>", "from": a, "to": b}`
//! (`from`/`to` optional absolute layer bounds inside the stage), answering
//! one JSON line with the output SHA-256 and single-thread forward time.

use std::io::{BufRead, Write};
use std::path::PathBuf;
use std::time::Instant;

use mycelium_mobile_core::qwen2::{Qwen2Config, Qwen2Stage};
use mycelium_mobile_core::safetensors::SafetensorsFile;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn run(stage: &Qwen2Stage, request: &Value) -> Result<Value, String> {
    let seq = request["seq"].as_u64().ok_or("seq")? as usize;
    let bytes = std::fs::read(request["input"].as_str().ok_or("input")?).map_err(|e| e.to_string())?;
    let input: Vec<f32> = bytes.chunks_exact(4).map(|c| f32::from_le_bytes(c.try_into().unwrap())).collect();
    let from = request["from"].as_u64().map_or(stage.start_layer(), |v| v as usize);
    let to = request["to"].as_u64().map_or(stage.end_layer_exclusive(), |v| v as usize);
    if from < stage.start_layer() || to > stage.end_layer_exclusive() || from >= to {
        return Err("layer_range_outside_stage".into());
    }
    let started = Instant::now();
    let mut x = input;
    for layer in from..to {
        x = stage.forward_layer(layer - stage.start_layer(), seq, &x).map_err(|e| e.0)?;
    }
    let forward_ms = started.elapsed().as_secs_f64() * 1e3;
    if x.iter().any(|v| !v.is_finite()) {
        return Err("stage_output_nonfinite".into());
    }
    let out: Vec<u8> = x.iter().flat_map(|v| v.to_le_bytes()).collect();
    std::fs::write(request["output"].as_str().ok_or("output")?, &out).map_err(|e| e.to_string())?;
    Ok(json!({"ok": true, "forward_ms": forward_ms, "layers": to - from, "seq": seq,
              "sha256": format!("sha256:{}", hex::encode(Sha256::digest(&out)))}))
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 4 {
        eprintln!("usage: qwen2_stage_exec <snapshot-dir> <start> <end>");
        std::process::exit(2);
    }
    let dir = PathBuf::from(&args[1]);
    let (start, end): (usize, usize) = (args[2].parse().unwrap(), args[3].parse().unwrap());
    let config: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("config.json")).unwrap()).unwrap();
    let config = Qwen2Config::from_hf_config(&config).unwrap();
    let mut file = SafetensorsFile::open(&dir.join("model.safetensors")).unwrap();
    let started = Instant::now();
    let stage = Qwen2Stage::load(config, start, end, &mut file).unwrap();
    let load_ms = started.elapsed().as_secs_f64() * 1e3;
    let mut stdout = std::io::stdout().lock();
    writeln!(stdout, "{}", json!({"ready": true, "load_ms": load_ms, "resident_bytes": stage.resident_bytes()})).unwrap();
    stdout.flush().unwrap();
    for line in std::io::stdin().lock().lines() {
        let line = line.unwrap();
        if line.trim().is_empty() {
            continue;
        }
        let reply = serde_json::from_str::<Value>(&line)
            .map_err(|e| e.to_string())
            .and_then(|r| run(&stage, &r))
            .unwrap_or_else(|e| json!({"ok": false, "error": e}));
        writeln!(stdout, "{reply}").unwrap();
        stdout.flush().unwrap();
    }
}
