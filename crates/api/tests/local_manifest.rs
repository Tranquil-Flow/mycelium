// SPDX-License-Identifier: AGPL-3.0-only
//! `api::local_manifest::compile_local_model_manifest` equals the executed `physical_deployment.
//! compile_local_model_manifest` (f4e366e9, Python 3.14) on 101 scripted local snapshot layouts:
//! `L10-evidence/r18-probe-manifest.py` (sha256 7ba0a3d72fd733ecc111b51d2c4e55073c07ecdad50492f0d30d2962bdd5c2ef)
//! -> `fixtures/oracle_manifest.json` (sha256 360b0714d0561af8dbec4c10624c5905aa3d9f8e0fc9348f4901d83e8bfc9ae7).
//! The probe stores the layouts as specs (tiny BF16 safetensors with the real Qwen2.5-0.5B tensor names, HF blob
//! links, corrupt headers, hostile index paths); this test rebuilds byte-identical files from them. Accepted
//! manifests are compared as canonical JSON (so the manifest digest, which hashes every file, is checked too);
//! refusals by the oracle's exact message. The real cached 988 MB snapshot is one more executed value
//! (`real_cached_snapshot_digest_equals_the_oracle`, `--ignored`, needs `~/mycelium-l3c-window-model-seed`).
use api::local_manifest::{CompileError, LocalModelSource, compile_local_model_manifest};
use serde_json::Value;
use std::{
    fs,
    os::unix::fs::symlink,
    path::{Path, PathBuf},
};

const FIXTURE: &str = include_str!("fixtures/oracle_manifest.json");

fn st_bytes(spec: &Value) -> Vec<u8> {
    let mut tensors: Vec<&Value> = spec["tensors"].as_array().unwrap().iter().collect();
    tensors.sort_by(|a, b| a[0].as_str().cmp(&b[0].as_str()));
    let shift = spec.get("shift").and_then(Value::as_i64).unwrap_or(0);
    let seed = spec.get("seed").and_then(Value::as_u64).unwrap_or(0);
    let per = |d: &str| match d {
        "BF16" | "F16" => 2usize,
        "F32" => 4,
        "U8" => 1,
        other => panic!("dtype {other}"),
    };
    let mut header = serde_json::Map::new();
    let (mut off, mut data) = (0usize, Vec::new());
    for (i, t) in tensors.iter().enumerate() {
        let dims: Vec<usize> = t[2]
            .as_array()
            .unwrap()
            .iter()
            .map(|d| d.as_u64().unwrap() as usize)
            .collect();
        let size = dims.iter().product::<usize>() * per(t[1].as_str().unwrap());
        header.insert(
            t[0].as_str().unwrap().to_owned(),
            serde_json::json!({"dtype": t[1], "shape": t[2],
                "data_offsets": [off as i64 + shift, (off + size) as i64 + shift]}),
        );
        data.extend((0..size).map(|j| ((seed as usize + i + j) % 251) as u8));
        off += size;
    }
    if let Some(metadata) = spec.get("metadata").filter(|m| !m.is_null()) {
        header.insert("__metadata__".into(), metadata.clone());
    }
    let header = serde_json::to_vec(&Value::Object(header)).unwrap();
    let trim = spec.get("trim").and_then(Value::as_u64).unwrap_or(0) as usize;
    let pad = spec.get("pad").and_then(Value::as_u64).unwrap_or(0) as usize;
    data.truncate(data.len() - trim);
    data.extend(vec![0u8; pad]);
    let mut out = (header.len() as u64).to_le_bytes().to_vec();
    out.extend(header);
    out.extend(data);
    out
}
fn write(root: &Path, rel: &str, spec: &Value, base: &Path) {
    let path = root.join(rel);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    match spec["kind"].as_str().unwrap() {
        "st" => fs::write(&path, st_bytes(spec)).unwrap(),
        "text" => fs::write(&path, spec["text"].as_str().unwrap()).unwrap(),
        "hex" => fs::write(&path, hex_to_bytes(spec["hex"].as_str().unwrap())).unwrap(),
        "symlink" => symlink(spec["target"].as_str().unwrap(), &path).unwrap(),
        "hfblob" => {
            let blobs = base.join("models--Qwen--Qwen2.5-0.5B-Instruct/blobs");
            fs::create_dir_all(&blobs).unwrap();
            let blob = spec["blob"].as_str().unwrap();
            write(&blobs, blob, &spec["of"], base);
            symlink(format!("../../blobs/{blob}"), &path).unwrap();
        }
        "mkdir" => fs::create_dir_all(&path).unwrap(),
        other => panic!("kind {other}"),
    }
}
fn hex_to_bytes(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}
fn canonical(value: &Value) -> Vec<u8> {
    protocol::canonical::to_bytes(value).unwrap()
}

#[test]
fn local_manifest_compile_equals_the_executed_oracle() {
    let doc: Value = serde_json::from_str(FIXTURE).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    let cases = doc["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 101);
    let (mut accepted, mut refused) = (0usize, 0usize);
    let mut failures = Vec::new();
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let base = fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!("l10-manifest-{}-{label}", std::process::id()));
        fs::create_dir_all(&base).unwrap();
        let commit = case["commit"].as_str().unwrap();
        let parent = case["parent"].as_str().unwrap();
        let root_dir: PathBuf = base
            .join("models--Qwen--Qwen2.5-0.5B-Instruct")
            .join(parent)
            .join(commit);
        fs::create_dir_all(&root_dir).unwrap();
        for (rel, spec) in case["files"].as_object().unwrap() {
            write(&root_dir, rel, spec, &base);
        }
        let root = match case["root"].as_str().unwrap() {
            "dir" => root_dir.clone(),
            "missing" => base.join("nope"),
            "symlink" => {
                symlink(&root_dir, base.join("lnk")).unwrap();
                base.join("lnk")
            }
            "file" => {
                fs::write(base.join("afile"), "x").unwrap();
                base.join("afile")
            }
            other => panic!("root {other}"),
        };
        let assets = match &case["tokenizer_assets"] {
            Value::Null => Ok(vec![]),
            Value::Array(a) => Ok(a.iter().map(|v| v.as_str().unwrap().to_owned()).collect()),
            _ => Err(()),
        };
        let source = LocalModelSource {
            root,
            model_id: case["model_id"].as_str().unwrap().into(),
            requested_revision: case["revision"].as_str().unwrap().into(),
            resolved_commit: commit.into(),
            tokenizer_assets: assets,
        };
        let got = compile_local_model_manifest(&source);
        let want = &case["result"];
        match (got, want["ok"].as_bool().unwrap()) {
            (Ok(manifest), true) => {
                accepted += 1;
                if canonical(&manifest) != canonical(&want["manifest"]) {
                    failures.push(format!(
                        "{label}: manifest differs\nours   {manifest}\noracle {}",
                        want["manifest"]
                    ));
                }
            }
            (Err(CompileError(message)), false) => {
                refused += 1;
                if message != want["error"].as_str().unwrap() {
                    failures.push(format!("{label}: {message:?} != {}", want["error"]));
                }
            }
            (got, ok) => failures.push(format!(
                "{label}: ours {:?}, oracle ok={ok} {}",
                got.map(|_| "manifest"),
                want["error"]
            )),
        }
        fs::remove_dir_all(&base).unwrap();
    }
    assert!(
        failures.is_empty(),
        "{} differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_eq!(
        (accepted, refused),
        (15, 86),
        "the oracle accepted 15 layouts and refused 86"
    );
}

/// The cached real snapshot: the same hash of every byte, the same manifest digest, as the Python compile.
#[test]
#[ignore = "hashes the 988 MB cached Qwen2.5-0.5B-Instruct snapshot; needs ~/mycelium-l3c-window-model-seed"]
fn real_cached_snapshot_digest_equals_the_oracle() {
    let doc: Value = serde_json::from_str(FIXTURE).unwrap();
    let real = &doc["real_snapshot"];
    let source = LocalModelSource {
        root: PathBuf::from(real["root"].as_str().unwrap()),
        model_id: "Qwen/Qwen2.5-0.5B-Instruct".into(),
        requested_revision: "main".into(),
        resolved_commit: "7ae557604adf67be50417f59c2c2f167def9a775".into(),
        tokenizer_assets: Ok(vec![]),
    };
    let manifest = compile_local_model_manifest(&source).unwrap();
    assert_eq!(manifest["manifest_digest"], real["manifest_digest"]);
    assert_eq!(manifest["files"], real["files"]);
    assert_eq!(manifest["num_layers"], real["num_layers"]);
}
