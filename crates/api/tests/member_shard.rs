// SPDX-License-Identifier: AGPL-3.0-only
//! Storage boundary only: real producer bytes, not joined/native serving proof.
use api::{compiler_inputs::member_shard::MemberShard, preferences::resource_policy};
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};
#[path = "support/member_shard_carrier.rs"]
mod carrier;

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture {
    root: PathBuf,
    recipe: Value,
    transfer: Value,
    bytes: Vec<u8>,
}
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "b7-receive-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        resource_policy::write(&root, &json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"})).unwrap();
        // Independently recorded source-slice corpus; not model or resident proof.
        let fixture: Value = serde_json::from_str(include_str!(
            "../../planner/tests/fixtures/source-slices.json"
        ))
        .unwrap();
        let case = &fixture["cases"][0];
        let recipe = case["node_plans"]["node-0"].clone();
        let transfer = case["node_files"]["node-0"][0].clone();
        let name = transfer["path"]
            .as_str()
            .unwrap()
            .strip_prefix("deployment/")
            .unwrap();
        let encoded = case["shard_files"][name].as_str().unwrap();
        let bytes = (0..encoded.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&encoded[i..i + 2], 16).unwrap())
            .collect();
        Self {
            root,
            recipe,
            transfer,
            bytes,
        }
    }
    fn begin(&self, run: &str) -> MemberShard {
        MemberShard::begin(
            &self.root,
            run,
            "node-0",
            &self.transfer,
            &self.recipe,
            self.recipe["node_plan_digest"].as_str().unwrap(),
        )
        .unwrap()
    }
    fn charge(&self) -> u64 {
        let v: Value = serde_json::from_slice(
            &fs::read(self.root.join("chain-runs/compiler-storage.private.json")).unwrap(),
        )
        .unwrap();
        v["entries"]
            .as_object()
            .unwrap()
            .values()
            .map(|v| v["bytes"].as_u64().unwrap())
            .sum()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}
#[test]
fn received_shard_retries_verify_and_refund_only_after_discard() {
    let f = Fixture::new();
    let mut receive = f.begin("receive");
    assert_eq!(f.charge(), f.bytes.len() as u64);
    let split = f.bytes.len() / 2;
    receive.write_chunk(0, &f.bytes[..split]).unwrap();
    receive.write_chunk(0, &f.bytes[..split]).unwrap();
    assert_eq!(receive.received_bytes().unwrap(), split as u64);
    receive
        .write_chunk(split as u64, &f.bytes[split..])
        .unwrap();
    receive.verify(&|| Ok(())).unwrap();
    assert_eq!(fs::read(receive.path()).unwrap(), f.bytes);
    let path = receive.path().to_owned();
    receive.discard().unwrap();
    assert!(!path.exists());
    assert_eq!(f.charge(), 0);
}
#[test]
fn truncated_tampered_and_conflicting_retry_do_not_become_retained() {
    let f = Fixture::new();
    let mut receive = f.begin("truncated");
    receive.write_chunk(0, &f.bytes[..1]).unwrap();
    assert_eq!(
        receive.verify(&|| Ok(())).unwrap_err().0,
        "member_shard_length_mismatch"
    );
    assert_eq!(
        receive.write_chunk(0, &[f.bytes[0] ^ 1]).unwrap_err().0,
        "member_shard_retry_mismatch"
    );
    receive.discard().unwrap();
    let mut receive = f.begin("tampered");
    let mut bad = f.bytes.clone();
    bad[0] ^= 1;
    receive.write_chunk(0, &bad).unwrap();
    assert_eq!(
        receive.verify(&|| Ok(())).unwrap_err().0,
        "member_shard_hash_mismatch"
    );
    assert_eq!(f.charge(), f.bytes.len() as u64);
    receive.discard().unwrap();
}
#[test]
fn recipe_pins_cancel_gaps_and_replaced_files_refuse_without_refund() {
    use api::compiler_inputs::Error;
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new();
    let digest = f.recipe["node_plan_digest"].as_str().unwrap();
    assert_eq!(
        MemberShard::begin(
            &f.root,
            "wrong-node",
            "node-1",
            &f.transfer,
            &f.recipe,
            digest
        )
        .unwrap_err()
        .0,
        "member_shard_recipe_invalid"
    );
    let mut receive = f.begin("negative");
    assert_eq!(
        receive.write_chunk(1, &[0]).unwrap_err().0,
        "member_shard_chunk_invalid"
    );
    receive.write_chunk(0, &f.bytes).unwrap();
    assert_eq!(
        receive
            .verify(&|| Err(Error("cancelled_by_owner")))
            .unwrap_err()
            .0,
        "cancelled_by_owner"
    );
    assert_eq!(f.charge(), f.bytes.len() as u64);
    let path = receive.path();
    let original = f.root.join("held-original");
    fs::rename(&path, &original).unwrap();
    fs::write(&path, &f.bytes).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(
        receive.discard().unwrap_err().0,
        "member_shard_identity_changed"
    );
    drop(receive);
    assert_eq!(
        MemberShard::resume(
            &f.root,
            "negative",
            "node-0",
            &f.transfer,
            &f.recipe,
            digest
        )
        .unwrap_err()
        .0,
        "member_shard_identity_changed"
    );
    assert_eq!(fs::read(&path).unwrap(), f.bytes);
    fs::remove_file(&path).unwrap();
    fs::rename(&original, &path).unwrap();
    let mut receive = MemberShard::resume(
        &f.root,
        "negative",
        "node-0",
        &f.transfer,
        &f.recipe,
        digest,
    )
    .unwrap();
    receive.discard().unwrap();
    assert_eq!(f.charge(), 0);
}

#[test]
fn pinned_but_wrong_recipe_ranges_refuse_even_when_transfer_matches() {
    let mut f = Fixture::new();
    let file = f.recipe["files"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|r| r["path"] == f.transfer["path"])
        .unwrap();
    // A real content digest of different bytes, not a fabricated native proof.
    file["segments"][0]["sha256"] =
        json!(protocol::artifact_grant::range_digest(b"different bytes"));
    f.recipe.as_object_mut().unwrap().remove("node_plan_digest");
    f.recipe["node_plan_digest"] = json!(protocol::canonical::digest(&f.recipe).unwrap());
    let mut receive = f.begin("bad-recipe");
    receive.write_chunk(0, &f.bytes).unwrap();
    assert_eq!(
        receive.verify(&|| Ok(())).unwrap_err().0,
        "member_shard_recipe_mismatch"
    );
    receive.discard().unwrap();
}

#[test]
fn replacing_stage_directory_with_symlink_never_deletes_foreign_bytes() {
    let f = Fixture::new();
    let mut receive = f.begin("link-attack");
    receive.write_chunk(0, &f.bytes).unwrap();
    let stages = receive.path().parent().unwrap().to_owned();
    let moved = f.root.join("foreign-shards");
    fs::rename(&stages, &moved).unwrap();
    std::os::unix::fs::symlink(&moved, &stages).unwrap();
    let result = receive.discard();
    assert_eq!(result.unwrap_err().0, "member_shard_identity_changed");
    assert_eq!(
        fs::read(moved.join(receive.path().file_name().unwrap())).unwrap(),
        f.bytes
    );
    fs::remove_file(&stages).unwrap();
    fs::rename(&moved, &stages).unwrap();
    receive.discard().unwrap();
}

#[test]
fn drop_keeps_debt_and_exclusive_custody_can_resume() {
    let f = Fixture::new();
    let mut receive = f.begin("resume");
    receive.write_chunk(0, &f.bytes[..1]).unwrap();
    assert_eq!(
        MemberShard::resume(
            &f.root,
            "resume",
            "node-0",
            &f.transfer,
            &f.recipe,
            f.recipe["node_plan_digest"].as_str().unwrap()
        )
        .unwrap_err()
        .0,
        "member_shard_busy"
    );
    drop(receive);
    assert_eq!(f.charge(), f.bytes.len() as u64);
    let mut receive = MemberShard::resume(
        &f.root,
        "resume",
        "node-0",
        &f.transfer,
        &f.recipe,
        f.recipe["node_plan_digest"].as_str().unwrap(),
    )
    .unwrap();
    assert_eq!(receive.received_bytes().unwrap(), 1);
    receive.write_chunk(1, &f.bytes[1..]).unwrap();
    receive.verify(&|| Ok(())).unwrap();
    receive.discard().unwrap();
    assert_eq!(f.charge(), 0);
}
