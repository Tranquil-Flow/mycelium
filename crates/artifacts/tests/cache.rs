// SPDX-License-Identifier: AGPL-3.0-only
#[path = "support/temp.rs"]
mod test_temp;
use artifacts::{ArtifactKey, Cache};
use budgets::Bytes;
use serde_json::Value;
use std::{fs, io::Read, path::PathBuf};
fn scratch() -> PathBuf {
    test_temp::directory("cache-test")
}
fn key() -> ArtifactKey {
    let f: Value = serde_json::from_str(include_str!(
        "../../planner/tests/fixtures/artifact-keys.json"
    ))
    .unwrap();
    serde_json::from_value(f[0]["key"].clone()).unwrap()
}
fn cache(root: &std::path::Path, cap: u64) -> artifacts::Result<Cache> {
    Cache::open(root, Bytes::new(cap), Bytes::new(65536), Bytes::new(4096))
}
#[test]
fn python_object_identity_including_unicode_is_exact() {
    let f: Value = serde_json::from_str(include_str!(
        "../../planner/tests/fixtures/artifact-keys.json"
    ))
    .unwrap();
    for row in f.as_array().unwrap() {
        let k: ArtifactKey = serde_json::from_value(row["key"].clone()).unwrap();
        assert_eq!(k.object_id().unwrap(), row["object_id"]);
    }
}
#[test]
fn offline_store_reopen_and_read_held_bytes() {
    let root = scratch();
    let source = root.join("source");
    fs::write(&source, b"cached-model-bytes").unwrap();
    let k = key();
    let c = cache(&root.join("cache"), 1024).unwrap();
    c.store(&k, &source).unwrap();
    drop(c);
    fs::remove_file(&source).unwrap();
    let c = cache(&root.join("cache"), 1024).unwrap();
    let mut held = c.verify(&k).unwrap();
    let mut bytes = Vec::new();
    held.file.read_to_end(&mut bytes).unwrap();
    assert_eq!(bytes, b"cached-model-bytes");
    drop(held);
    drop(c);
    fs::remove_dir_all(root).unwrap();
}
#[test]
fn corrupt_partial_and_foreign_objects_refuse_without_fallback() {
    let root = scratch();
    let source = root.join("source");
    fs::write(&source, b"cached-model-bytes").unwrap();
    let k = key();
    let c = cache(&root.join("cache"), 1024).unwrap();
    c.store(&k, &source).unwrap();
    let object = root
        .join("cache/objects")
        .join(&k.object_id().unwrap()[7..]);
    let mut foreign = k.clone();
    foreign.model_revision = "foreign".into();
    assert_eq!(c.verify(&foreign).err().unwrap().code, "artifact_missing");
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(&object, fs::Permissions::from_mode(0o600)).unwrap();
    fs::write(&object, b"cached-model-byteX").unwrap();
    assert_eq!(c.verify(&k).err().unwrap().code, "artifact_corrupt");
    fs::write(&object, b"partial").unwrap();
    assert_eq!(c.verify(&k).err().unwrap().code, "artifact_corrupt");
    fs::remove_file(&object).unwrap();
    assert_eq!(c.verify(&k).err().unwrap().code, "artifact_missing");
    drop(c);
    fs::remove_dir_all(root).unwrap();
}
#[test]
fn symlink_cache_and_object_refuse() {
    use std::os::unix::fs::symlink;
    let root = scratch();
    let source = root.join("source");
    fs::write(&source, b"cached-model-bytes").unwrap();
    let k = key();
    let c = cache(&root.join("cache"), 1024).unwrap();
    c.store(&k, &source).unwrap();
    symlink(root.join("cache"), root.join("alias")).unwrap();
    assert!(cache(&root.join("alias"), 1024).is_err());
    let object = root
        .join("cache/objects")
        .join(&k.object_id().unwrap()[7..]);
    fs::remove_file(&object).unwrap();
    symlink(&source, &object).unwrap();
    assert_eq!(c.verify(&k).err().unwrap().code, "artifact_unsafe");
    drop(c);
    fs::remove_dir_all(root).unwrap();
}
#[test]
fn unsafe_index_and_links_refuse_and_unindexed_files_are_ignored_without_mutating_foreign_bytes() {
    use std::os::unix::fs::symlink;
    let root = scratch();
    let source = root.join("source");
    fs::write(&source, b"cached-model-bytes").unwrap();
    let k = key();
    let c = cache(&root.join("cache"), 1024).unwrap();
    c.store(&k, &source).unwrap();
    let path = root.join("cache/index.v1.json");
    let clean = fs::read(&path).unwrap();
    fs::write(&path, b"{\"protocol\":\"x\",\"protocol\":\"y\"}").unwrap();
    assert_eq!(c.verify(&k).unwrap_err().code, "artifact_index_invalid");
    fs::write(&path, &clean).unwrap();
    // Round 7 (V02-PLAN 1.1 / integrator ruling a): v0.1.4 ignores an unindexed file in objects/
    // (never served, never deleted), so it no longer refuses the whole cache. The indexed
    // object keeps serving and the foreign bytes are untouched.
    let orphan = root.join("cache/objects/interrupted.partial");
    fs::write(&orphan, b"must-not-delete").unwrap();
    assert!(c.verify(&k).is_ok());
    assert_eq!(fs::read(&orphan).unwrap(), b"must-not-delete");
    fs::remove_file(&orphan).unwrap();
    let mut bad: Value = serde_json::from_slice(&clean).unwrap();
    let id = k.object_id().unwrap();
    // Round 8 (ruling 5): v0.1.4 never reads `relative_path`; it locates bytes from the key it is
    // asked for. A forged path therefore cannot redirect anything and is not a refusal.
    bad["entries"][&id]["relative_path"] = serde_json::json!("../source");
    fs::write(&path, serde_json::to_vec(&bad).unwrap()).unwrap();
    assert!(c.verify(&k).is_ok());
    assert_eq!(fs::read(&source).unwrap(), b"cached-model-bytes");
    // A malformed id names a file under `objects/`; v0.1.4's eviction would unlink outside the
    // cache with it, so it stays a refusal (proof: cache-compat `edited-entry-traversal_id`).
    let entry = bad["entries"][&id].clone();
    bad["entries"] = serde_json::json!({"sha256:../../source": entry});
    fs::write(&path, serde_json::to_vec(&bad).unwrap()).unwrap();
    assert_eq!(c.verify(&k).unwrap_err().code, "artifact_index_invalid");
    assert_eq!(fs::read(&source).unwrap(), b"cached-model-bytes");
    fs::remove_file(&path).unwrap();
    symlink(&source, &path).unwrap();
    assert_eq!(c.verify(&k).unwrap_err().code, "artifact_unsafe");
    drop(c);
    fs::remove_dir_all(root).unwrap();
}
#[test]
fn invalid_identity_refuses_and_an_oversized_object_is_evicted_at_once_as_in_v014() {
    let root = scratch();
    let source = root.join("source");
    fs::write(&source, b"cached-model-bytes").unwrap();
    let k = key();
    let c = cache(&root.join("cache"), 16).unwrap();
    // Round 7 (ruling a): v0.1.4 copies the object in, then evicts least-recently-used unpinned
    // entries until the cap holds. A single object over the cap is therefore reported
    // "populated" and is gone again (oracle vector `single-object-larger-than-capacity`).
    assert_eq!(
        c.store(&k, &source).unwrap(),
        artifacts::StoreOutcome::Populated
    );
    assert_eq!(fs::read_dir(root.join("cache/objects")).unwrap().count(), 0);
    assert_eq!(c.status().unwrap()["entry_count"], 0);
    let mut bad = k.clone();
    bad.tensor_digest = "sha256:../foreign".into();
    assert_eq!(bad.object_id().unwrap_err().code, "artifact_key_invalid");
    drop(c);
    fs::remove_dir_all(root).unwrap();
}
