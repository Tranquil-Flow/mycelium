// SPDX-License-Identifier: AGPL-3.0-only
//! Model-free layout/admission fixture: zero bytes, never native model evidence.
use backend_mlx::{admission, stage_shard};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use stage_contract::{NumericsProfile, native_shard as contract};
use std::{
    fs,
    io::{Read, Seek, SeekFrom, Write},
    path::PathBuf,
};
struct Fixture(PathBuf);
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).expect("fixture cleanup");
    }
}
fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}
fn base64(bytes: &[u8]) -> String {
    const A: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for c in bytes.chunks(3) {
        let (a, b, d) = (c[0], *c.get(1).unwrap_or(&0), *c.get(2).unwrap_or(&0));
        out.push(A[(a >> 2) as usize] as char);
        out.push(A[(((a & 3) << 4) | (b >> 4)) as usize] as char);
        out.push(if c.len() > 1 {
            A[(((b & 15) << 2) | (d >> 6)) as usize] as char
        } else {
            '='
        });
        out.push(if c.len() > 2 {
            A[(d & 63) as usize] as char
        } else {
            '='
        });
    }
    out
}
fn repin(shard: &mut Value) {
    let recipe = &mut shard["recipe"];
    recipe.as_object_mut().unwrap().remove("node_plan_digest");
    let pin = protocol::canonical::digest(recipe).unwrap();
    recipe["node_plan_digest"] = json!(pin);
    shard["recipe_digest"] = json!(pin);
}
#[test]
fn shard_only_admission_checks_transfer_recipe_and_exact_tensor_ownership() {
    let path = std::env::temp_dir().join(format!("b6-layout-{}", std::process::id()));
    fs::create_dir(&path).unwrap();
    let fixture = Fixture(path.canonicalize().unwrap());
    let path = fixture.0.join("stage.safetensors");
    let d =
        admission::qwen3_descriptor(1..2, false, false, NumericsProfile::MlxPackedInt8).unwrap();
    let mut header = serde_json::Map::new();
    let mut offset = 0;
    for (name, shape) in model::Qwen2::weights(&d).unwrap() {
        let bytes = shape.iter().fold(2u64, |n, d| n * (*d as u64));
        header.insert(
            name,
            json!({"dtype":"BF16","shape":shape,"data_offsets":[offset,offset+bytes]}),
        );
        offset += bytes;
    }
    let mut raw = serde_json::to_vec(&header).unwrap();
    raw.resize(raw.len().div_ceil(8) * 8, b' ');
    let mut bytes = (raw.len() as u64).to_le_bytes().to_vec();
    bytes.extend(&raw);
    let prefix = bytes.len();
    bytes.resize(prefix + offset as usize, 0);
    fs::write(&path, &bytes).unwrap();
    let transfer = json!({"path":"deployment/stage.safetensors","size_bytes":bytes.len(),"content_digest":digest(&bytes)});
    let cap = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63
        .bytes()
        .unwrap()
        .get() as usize;
    let mut segments = vec![
        json!({"offset":0,"length":prefix,"sha256":digest(&bytes[..prefix]),"inline_base64":base64(&bytes[..prefix])}),
    ];
    for (i, chunk) in bytes[prefix..].chunks(cap).enumerate() {
        segments.push(json!({"offset":prefix+i*cap,"length":chunk.len(),"sha256":digest(chunk),"source_path":"model.safetensors","source_offset":i*cap}));
    }
    let source_hash = d
        .checkpoint
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    let mut row = transfer.clone();
    row["segments"] = json!(segments);
    let recipe = json!({"protocol":"mycelium.a13_node_slice_plan.v1","node_id":"node-0","model_id":"Qwen/Qwen3-0.6B","resolved_commit":d.revision,"files":[row],"source_files":[{"path":"model.safetensors","size_bytes":admission::artifact_layout(&d).unwrap().0,"content_digest":format!("sha256:{source_hash}")} ]});
    let mut shard = json!({"protocol":contract::PROTOCOL,"range":[1,2],"node_id":"node-0","transfer":transfer,"recipe":recipe});
    repin(&mut shard);
    assert!(!fixture.0.join("model.safetensors").exists());
    stage_shard::verify(&path, &d, &shard).unwrap();
    let mut wrong = shard.clone();
    wrong["range"] = json!([2, 3]);
    assert_eq!(
        stage_shard::verify(&path, &d, &wrong).unwrap_err().phase,
        contract::BINDING
    );
    let mut f = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&path)
        .unwrap();
    f.seek(SeekFrom::End(-1)).unwrap();
    let mut last = [0];
    f.read_exact(&mut last).unwrap();
    f.seek(SeekFrom::End(-1)).unwrap();
    f.write_all(&[1]).unwrap();
    assert_eq!(
        stage_shard::verify(&path, &d, &shard).unwrap_err().phase,
        contract::HASH
    );
    f.set_len(bytes.len() as u64 - 1).unwrap();
    assert_eq!(
        stage_shard::verify(&path, &d, &shard).unwrap_err().phase,
        contract::LENGTH
    );
    f.seek(SeekFrom::End(0)).unwrap();
    f.write_all(&last).unwrap();
    let mut wrong = shard.clone();
    wrong["recipe"]["files"][0]["segments"][1]["sha256"] = json!(digest(b"wrong"));
    repin(&mut wrong);
    assert_eq!(
        stage_shard::verify(&path, &d, &wrong).unwrap_err().phase,
        contract::RECIPE
    );
    let mut wrong = shard.clone();
    wrong["recipe"]["files"][0]["segments"][0]["inline_base64"] = json!("wrong");
    repin(&mut wrong);
    assert_eq!(
        stage_shard::verify(&path, &d, &wrong).unwrap_err().phase,
        contract::RECIPE
    );
    let other =
        admission::qwen3_descriptor(2..3, false, false, NumericsProfile::MlxPackedInt8).unwrap();
    let mut wrong = shard.clone();
    wrong["range"] = json!([2, 3]);
    assert_eq!(
        stage_shard::verify(&path, &other, &wrong)
            .unwrap_err()
            .phase,
        contract::TENSORS
    );
    stage_shard::verify(&path, &d, &shard).unwrap();
}
