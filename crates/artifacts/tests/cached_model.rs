// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit cached-byte qualification only: no download, tokenizer or inference.
use artifacts::{ArtifactKey, Cache};
use budgets::Bytes;
use std::{fs, io::Read, os::unix::fs::DirBuilderExt, path::PathBuf};
#[test]
#[ignore = "requires the already-cached pinned L8 Qwen2 file and private scratch"]
fn real_cached_qwen2_offline_reopen_keeps_verified_handle() {
    let source = PathBuf::from(
        std::env::var_os("MYCELIUM_QWEN2_CHECKPOINT")
            .expect("BLOCKED: cached checkpoint required; no network fallback"),
    );
    let scratch = fs::canonicalize(std::env::temp_dir()).unwrap();
    let root = scratch.join(format!("v02-l7-real-cache-{}", std::process::id()));
    fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
    // Source identities from base cfd1f515 crates/backend-mlx/src/admission.rs.
    let key = ArtifactKey {
        model_revision: "7ae557604adf67be50417f59c2c2f167def9a775".into(),
        manifest_digest: "sha256:65d3f3bf8256224d5ecc7dc63e12c2211717a116cb98428648e5f1fe952a14d5"
            .into(),
        format: "safetensors".into(),
        quantization: "none".into(),
        tensor_digest: "sha256:fdf756fa7fcbe7404d5c60e26bff1a0c8b8aa1f72ced49e7dd0210fe288fb7fe"
            .into(),
        size_bytes: 988_097_824,
    };
    let cap = Bytes::new(key.size_bytes * 2);
    let index = Bytes::new(65536);
    let chunk = Bytes::new(1 << 20);
    let before = fs::metadata(&source).unwrap();
    assert_eq!(before.len(), key.size_bytes);
    let cache = Cache::open(&root, cap, index, chunk).unwrap();
    cache.store(&key, &source).unwrap();
    drop(cache);
    let cache = Cache::open(&root, cap, index, chunk).unwrap();
    let mut held = cache.verify(&key).unwrap();
    assert_eq!(held.key(), &key);
    let mut length = [0; 8];
    held.file.read_exact(&mut length).unwrap();
    let header = u64::from_le_bytes(length);
    assert!(header > 0 && header < key.size_bytes);
    let after = fs::metadata(&source).unwrap();
    assert_eq!(before.len(), after.len());
    assert_eq!(before.modified().unwrap(), after.modified().unwrap());
    println!(
        "{{\"verified_bytes\":{},\"tensor_digest\":\"{}\",\"offline_reopen\":true,\"model_execution\":false}}",
        key.size_bytes, key.tensor_digest
    );
    drop(held);
    drop(cache);
    fs::remove_dir_all(root).unwrap();
}
