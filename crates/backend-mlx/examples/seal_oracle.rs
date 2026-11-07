// SPDX-License-Identifier: AGPL-3.0-only
//! Seal existing independent oracle arrays; never run an oracle interpreter.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{fs, path::Path};
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 3 {
        return Err("usage: seal_oracle <immutable-spike-root> <output.json>".into());
    }
    let root = Path::new(&args[1]);
    let seal_bytes = fs::read(root.join("SHA256SUMS.json"))?;
    if format!("{:x}", Sha256::digest(&seal_bytes))
        != "756d5b371fec1ea38035a507664ff8fb7bbc92dfc65574fa9ac29e22b9ca9387"
    {
        return Err("source seal differs from spike commit 2e0dd013".into());
    }
    let seals: Vec<Value> = serde_json::from_slice(&seal_bytes)?;
    let checked = |name: &str| -> Result<Vec<u8>, Box<dyn std::error::Error>> {
        let bytes = fs::read(root.join(name))?;
        let seal = seals
            .iter()
            .find(|s| s["path"] == name)
            .ok_or("missing seal")?;
        if format!("{:x}", Sha256::digest(&bytes))
            != seal["sha256"].as_str().ok_or("digest field")?
            || bytes.len() as u64 != seal["bytes"].as_u64().ok_or("size field")?
        {
            return Err("source seal mismatch".into());
        }
        Ok(bytes)
    };
    let run: Value = serde_json::from_slice(&checked("results/python/run.json")?)?;
    let mut workload: Value = serde_json::from_slice(&checked("workload.json")?)?;
    workload
        .as_object_mut()
        .ok_or("workload object")?
        .remove("model_file");
    let mut arrays = serde_json::Map::new();
    let mut files: Vec<String> = (0..64)
        .flat_map(|i| {
            [
                format!("stage8-{i}.npy"),
                format!("stage24-{i}.npy"),
                format!("logits-{i}.npy"),
            ]
        })
        .collect();
    for i in 0..24 {
        files.push(format!("kv-{i}-k.npy"));
        files.push(format!("kv-{i}-v.npy"));
    }
    for name in files {
        let bytes = checked(&format!("results/python/{name}"))?;
        if bytes.get(..8) != Some(b"\x93NUMPY\x01\x00") {
            return Err("expected NPY v1".into());
        }
        let n = usize::from(u16::from_le_bytes([bytes[8], bytes[9]]));
        let header = std::str::from_utf8(bytes.get(10..10 + n).ok_or("header bounds")?)?;
        if !header.contains("'descr': '<f4'") {
            return Err("NPY dtype".into());
        }
        let dims = header
            .split("'shape': (")
            .nth(1)
            .ok_or("shape")?
            .split(')')
            .next()
            .ok_or("shape close")?;
        let shape: Vec<u64> = dims
            .split(',')
            .filter(|s| !s.trim().is_empty())
            .map(|s| s.trim().parse())
            .collect::<Result<_, _>>()?;
        // A vector with singleton axes has identical C/Fortran byte order.
        if !(header.contains("'fortran_order': False")
            || (header.contains("'fortran_order': True")
                && shape.iter().filter(|d| **d > 1).count() <= 1))
        {
            return Err("NPY noncanonical order".into());
        }
        let payload = &bytes[10 + n..];
        let size = shape
            .iter()
            .try_fold(4u64, |n, d| n.checked_mul(*d))
            .ok_or("shape overflow")?;
        if size != payload.len() as u64 {
            return Err("NPY size".into());
        }
        for word in payload.chunks_exact(4) {
            if !f32::from_le_bytes([word[0], word[1], word[2], word[3]]).is_finite() {
                return Err("nonfinite oracle".into());
            }
        }
        if name.starts_with("kv-") {
            if shape != [1, 2, 98, 64] {
                return Err("terminal KV geometry".into());
            }
            // Product attention caches append along sequence; prior values never
            // change. Project each head's prefix, NOT a flat buffer prefix.
            for position in 35..98usize {
                let mut prefix = Vec::new();
                for head in 0..2usize {
                    let start = head * 98 * 64 * 4;
                    prefix.extend_from_slice(&payload[start..start + position * 64 * 4]);
                }
                arrays.insert(format!("{}-after-{position}",name.trim_end_matches(".npy")),json!({"shape":[1,2,position,64],"dtype":"float32-le","payload_bytes":prefix.len(),"payload_sha256":format!("{:x}",Sha256::digest(&prefix)),"derived_from":name,"projection":"per-head sequence prefix of append-only terminal KV; no arithmetic"}));
            }
        }
        arrays.insert(name,json!({"shape":shape,"dtype":"float32-le","payload_bytes":payload.len(),"payload_sha256":format!("{:x}",Sha256::digest(payload)),"npy_sha256":format!("{:x}",Sha256::digest(&bytes))}));
    }
    let fixture = json!({"schema":"qwen2-mlx-explicit-f32-oracle-v1","source_spike":"2e0dd01349f886ecce4c68a51870adb900c7565a","source_seal_sha256":"756d5b371fec1ea38035a507664ff8fb7bbc92dfc65574fa9ac29e22b9ca9387","recorded_oracle":"52d1b69fb98e85529537500133ce6301c612d093","accepted_oracle":"27fdd49de7e18b58affa85b8a31e9a734d7b4d1e","oracle_module_sha256":"b248c09612e0bb10b238f0a00f84b1eaa8e1fd3c30f5864e77b82ee88d4b30c5","oracle_mlx":"0.31.1","candidate_mlx":"0.32.4","claim":"Same-module-byte differential oracle; not independent author-reference or cross-device proof","workload":workload,"feeds":run["feeds"],"tokens":run["tokens"],"arrays":arrays});
    fs::write(&args[2], serde_json::to_vec_pretty(&fixture)?)?;
    println!(
        "sealed {} arrays; original source seals verified",
        fixture["arrays"].as_object().ok_or("arrays")?.len()
    );
    Ok(())
}
