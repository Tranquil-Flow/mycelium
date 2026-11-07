// SPDX-License-Identifier: AGPL-3.0-only
//! Independently verify captured NPY payloads and provenance; no interpreter.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{fs, path::Path};
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn main() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 5 {
        return Err(
            "usage: verify_recorded <capture-dir> <candidate-fixture> <oracle-root> <generator>"
                .into(),
        );
    }
    let dir = Path::new(&args[1]);
    let recorded = fs::read(dir.join("fixture.json"))?;
    if recorded != fs::read(&args[2])? {
        return Err("candidate fixture differs from recorded fixture".into());
    }
    let fixture: Value = serde_json::from_slice(&recorded)?;
    let moe = match fixture["schema"].as_str() {
        Some("l8-recorded-oracle-v2")
            if fixture["oracle_commit"] == "941765e882ed3eea9a5576cc53e525c4caa20300" =>
        {
            false
        }
        Some("l8-moe-decoder-component-v1")
            if fixture["oracle_commit"] == "f4e366e98a1f705dfaf182897044500d290dbd7f" =>
        {
            true
        }
        Some("l8-moe-head-component-v1" | "l8-moe-entry-stage-component-v1")
            if fixture["oracle_commit"] == "f4e366e98a1f705dfaf182897044500d290dbd7f" =>
        {
            true
        }
        _ => return Err("oracle identity".into()),
    };
    let head = fixture["schema"] == "l8-moe-head-component-v1";
    let entry = fixture["schema"] == "l8-moe-entry-stage-component-v1";
    if fixture["generator_sha256"] != hash(&fs::read(&args[4])?) {
        return Err("generator differs".into());
    }
    let sources = [
        "mycelium_router/mlx_runtime.py",
        "numpy_runtime.py",
        "runtime_loader.py",
        "weight_quantization.py",
        "qwen35_mlx.py",
        "qwen35_shapes.py",
    ];
    let source_key = if moe { "sources" } else { "oracle_sources" };
    if fixture[source_key].as_object().ok_or("source map")?.len() != sources.len() {
        return Err("source inventory".into());
    }
    for name in sources {
        if fixture[source_key][name] != hash(&fs::read(Path::new(&args[3]).join(name))?) {
            return Err(format!("source changed: {name}").into());
        }
    }
    let arrays = fixture["arrays"].as_object().ok_or("array map")?;
    if head {
        if fixture["scope"]
            != "embedding-final-norm-untied-head-not-resident-stage-model-or-transport-admission"
            || fixture["rows"] != json!([3, 1, 1, 5])
            || fixture["tie_word_embeddings"] != false
            || fixture["vocab"] != 248320
            || fixture["hidden"] != 2048
        {
            return Err("MoE head workload".into());
        }
        let mut names = std::collections::BTreeSet::from(["embedding-lookup".to_owned()]);
        for step in 0..4 {
            for key in ["final-norm-all", "all-logits", "last-logits"] {
                names.insert(format!("step-{step}-{key}"));
            }
        }
        if names != arrays.keys().cloned().collect() {
            return Err("MoE head record inventory".into());
        }
    } else if entry {
        if fixture["scope"]
            != "entry-embedding-layers-0-1-not-resident-stage-model-or-transport-admission"
            || fixture["layers"] != 2
            || fixture["positions"] != json!([0, 3, 4, 0])
            || fixture["rows"] != json!([3, 1, 1, 5])
            || fixture["step_ids"]
                != json!([
                    [9707, 11, 1879],
                    [151644],
                    [248043],
                    [9707, 11, 1879, 151644, 248043]
                ])
        {
            return Err("MoE entry workload".into());
        }
        let mut names = std::collections::BTreeSet::new();
        for step in 0..4 {
            names.insert(format!("step-{step}-embedding"));
            for layer in 0..2 {
                for key in ["output", "state-0", "state-1"] {
                    names.insert(format!("layer-{layer}-step-{step}-{key}"));
                }
            }
        }
        if names != arrays.keys().cloned().collect() {
            return Err("MoE entry record inventory".into());
        }
    } else if moe {
        if fixture["layers"] != 40
            || fixture["positions"] != json!([0, 3, 4, 0])
            || fixture["rows"] != json!([3, 1, 1, 5])
            || fixture["host_split_boundaries"] != json!([1, 3, 20, 39])
            || fixture["scope"]
                != "streamed-hidden-decoder-not-resident-stage-model-or-transport-admission"
        {
            return Err("MoE component workload".into());
        }
        let mut names = std::collections::BTreeSet::new();
        for step in 0..4 {
            names.insert(format!("input-{step}"));
        }
        for layer in 0..40 {
            for step in 0..4 {
                for key in [
                    "output",
                    "probabilities",
                    "indices",
                    "scores",
                    "state-0",
                    "state-1",
                ] {
                    names.insert(format!("layer-{layer}-step-{step}-{key}"));
                }
            }
        }
        if names != arrays.keys().cloned().collect() {
            return Err("MoE record inventory".into());
        }
    } else {
        let expected = 64 * (3 + fixture["layers"].as_u64().ok_or("layers")? * 2)
            + 5
            + fixture["layers"].as_u64().ok_or("layers")? * 2;
        if arrays.len() as u64 != expected
            || fixture["tokens"].as_array().ok_or("tokens")?.len() != 64
            || fixture["feeds"].as_array().ok_or("feeds")?.len() != 64
        {
            return Err("record inventory".into());
        }
    }
    let mut total = 0u64;
    for (name, entry) in arrays {
        if name.is_empty()
            || !name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        {
            return Err("unsafe array name".into());
        }
        let file = dir.join(format!("{name}.npy"));
        let size = entry["payload_bytes"].as_u64().ok_or("payload size")?;
        if fs::metadata(&file)?.len() > size.checked_add(1024).ok_or("file cap overflow")? {
            return Err("NPY file cap".into());
        }
        let bytes = fs::read(file)?;
        if bytes.get(..8) != Some(b"\x93NUMPY\x01\x00") || bytes.len() < 10 {
            return Err("NPY format".into());
        }
        let n = usize::from(u16::from_le_bytes([bytes[8], bytes[9]]));
        let header = std::str::from_utf8(bytes.get(10..10 + n).ok_or("NPY header bounds")?)?;
        let indices = moe && name.ends_with("-indices");
        let dtype = if indices { "uint32" } else { "float32" };
        let descr = if indices {
            "'descr': '<u4'"
        } else {
            "'descr': '<f4'"
        };
        if !header.contains(descr) || entry["dtype"] != dtype {
            return Err("NPY dtype".into());
        }
        let dims = header
            .split("'shape': (")
            .nth(1)
            .ok_or("shape")?
            .split(')')
            .next()
            .ok_or("shape end")?;
        let shape: Vec<u64> = dims
            .split(',')
            .filter(|s| !s.trim().is_empty())
            .map(|s| s.trim().parse())
            .collect::<std::result::Result<_, _>>()?;
        if entry["shape"] != json!(shape) {
            return Err("NPY shape".into());
        }
        if !(header.contains("'fortran_order': False")
            || (header.contains("'fortran_order': True")
                && shape.iter().filter(|d| **d > 1).count() <= 1))
        {
            return Err("NPY noncanonical order".into());
        }
        let payload = &bytes[10 + n..];
        if payload.len() as u64 != size
            || shape
                .iter()
                .try_fold(4u64, |n, d| n.checked_mul(*d))
                .ok_or("shape overflow")?
                != size
            || entry["payload_sha256"] != hash(payload)
        {
            return Err(format!("NPY payload differs: {name}").into());
        }
        if !indices
            && payload
                .chunks_exact(4)
                .any(|b| !f32::from_le_bytes([b[0], b[1], b[2], b[3]]).is_finite())
        {
            return Err("nonfinite fixture".into());
        }
        total = total.checked_add(size).ok_or("total overflow")?;
    }
    println!(
        "{}",
        json!({"verified_arrays":arrays.len(),"payload_bytes":total,"fixture_sha256":hash(&recorded),"sources":sources.len(),"interpreter_invoked":false})
    );
    Ok(())
}
