// SPDX-License-Identifier: AGPL-3.0-only
//! Shared recorded-oracle helpers for the Qwen3.6 MoE native tests.
#![allow(dead_code)]
use backend_mlx::{Array, Context};
use model::{Tensor, Weight};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    rc::Rc,
};

pub fn payload(expected: &Value, a: &Array) {
    assert_eq!(expected["shape"], json!(a.shape()));
    let words = if expected["dtype"] == "uint32" {
        a.words().unwrap()
    } else {
        assert_eq!(expected["dtype"], "float32");
        a.floats().unwrap().into_iter().map(f32::to_bits).collect()
    };
    assert_eq!(expected["payload_bytes"], json!(words.len() * 4));
    let mut hash = Sha256::new();
    for w in words {
        hash.update(w.to_le_bytes());
    }
    assert_eq!(
        expected["payload_sha256"],
        json!(format!("{:x}", hash.finalize()))
    );
}
pub fn quiet() {
    assert_eq!(
        std::process::Command::new("/usr/sbin/sysctl")
            .args(["-n", "kern.memorystatus_vm_pressure_level"])
            .output()
            .unwrap()
            .stdout,
        b"1\n"
    );
    let demo = PathBuf::from(std::env::var("HOME").unwrap()).join("mycelium-demo-run");
    if demo.exists() {
        assert!(
            std::fs::read_dir(demo).unwrap().all(|e| !e
                .unwrap()
                .file_name()
                .to_string_lossy()
                .ends_with(".busy"))
        );
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
pub fn load_weights(
    ctx: &Rc<Context>,
    root: &Path,
    fixture: &Value,
    wanted: &BTreeMap<String, Vec<i32>>,
    profile: &str,
) -> BTreeMap<String, Weight<Array>> {
    let mut weights = BTreeMap::new();
    let mut by_shard: BTreeMap<String, BTreeMap<String, Vec<i32>>> = BTreeMap::new();
    for (name, shape) in wanted {
        let info = &fixture["weights"][name];
        assert_eq!(info["shape"], json!(shape));
        by_shard
            .entry(info["shard"].as_str().unwrap().to_owned())
            .or_default()
            .insert(name.clone(), shape.clone());
    }
    for (shard, names) in by_shard {
        quiet();
        let path = root.join(&shard);
        let identity = &fixture["checkpoint"]["shards"][&shard];
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
        for name in names.keys() {
            let meta = &header[name];
            let info = &fixture["weights"][name];
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
        }
        if profile == "f32" {
            weights.extend(
                ctx.load(path.to_str().unwrap(), &names)
                    .unwrap()
                    .into_iter()
                    .map(|(k, v)| (k, Weight::Float(v))),
            );
        } else {
            weights.extend(ctx.load_packed(path.to_str().unwrap(), &names).unwrap());
        }
    }
    assert_eq!(weights.len(), wanted.len());
    for (name, w) in &weights {
        let expected = &fixture["weights"][name]["resident"];
        match w {
            Weight::Float(a) => {
                assert_eq!(expected.as_object().unwrap().len(), 1);
                payload(&expected["float"], a);
            }
            Weight::Packed {
                codes,
                scales,
                group_scales,
                group_biases,
            } => {
                assert_eq!(expected.as_object().unwrap().len(), 4);
                for (key, a) in [
                    ("packed", codes),
                    ("scales", scales),
                    ("group_scales", group_scales),
                    ("group_biases", group_biases),
                ] {
                    payload(&expected[key], a);
                }
            }
        }
    }
    weights
}

/// Minimal NPY v1 reader for the recorded little-endian float32 oracle arrays; the payload
/// hash/shape must equal the fixture entry before any value is used.
pub fn npy_f32(path: &Path, expected: &Value) -> Vec<f32> {
    let bytes = std::fs::read(path).expect("recorded oracle array");
    assert_eq!(&bytes[..8], b"\x93NUMPY\x01\x00");
    let n = usize::from(u16::from_le_bytes([bytes[8], bytes[9]]));
    let header = std::str::from_utf8(&bytes[10..10 + n]).unwrap();
    assert!(header.contains("'descr': '<f4'") && header.contains("'fortran_order': False"));
    let dims: Vec<i64> = header
        .split("'shape': (")
        .nth(1)
        .unwrap()
        .split(')')
        .next()
        .unwrap()
        .split(',')
        .filter(|s| !s.trim().is_empty())
        .map(|s| s.trim().parse().unwrap())
        .collect();
    assert_eq!(expected["shape"], json!(dims));
    let body = &bytes[10 + n..];
    assert_eq!(expected["payload_bytes"], json!(body.len()));
    assert_eq!(
        expected["payload_sha256"],
        json!(format!("{:x}", Sha256::digest(body)))
    );
    body.chunks_exact(4)
        .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
        .collect()
}
pub fn checkpoint_root() -> PathBuf {
    PathBuf::from(
        std::env::var("MYCELIUM_MOE_CHECKPOINT").expect("cached snapshot; never download"),
    )
}
