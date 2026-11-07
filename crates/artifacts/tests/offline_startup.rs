// SPDX-License-Identifier: AGPL-3.0-only
//! Offline cached startup (L7 Done-when "offline cached startup works") at the artifact
//! layer: reopen a populated cache with the network layer absent, hold verified files, and
//! refuse specifically for missing / foreign / corrupt / truncated objects. The recorded
//! materialization fixture supplies real digests and payloads.
#[path = "support/temp.rs"]
mod test_temp;
use artifacts::{ArtifactKey, Cache, Unavailable};
use budgets::Bytes;
use serde_json::Value;
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::PathBuf,
};

struct Temp(PathBuf);
impl Drop for Temp {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn open(root: &std::path::Path) -> Cache {
    Cache::open(root, Bytes::new(10000), Bytes::new(65536), Bytes::new(4096)).unwrap()
}
fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/materialization.json")).unwrap()
}
fn payload(row: &Value) -> Vec<u8> {
    let hex = row["payload_hex"].as_str().unwrap();
    (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
        .collect()
}
fn key(row: &Value) -> ArtifactKey {
    serde_json::from_value(row["key"].clone()).unwrap()
}
/// Populate with the first two fixture objects; return their keys/payloads.
fn populated(t: &Temp) -> Vec<(ArtifactKey, Vec<u8>)> {
    let f = fixture();
    let cache = open(&t.0.join("cache"));
    let mut out = Vec::new();
    for (i, row) in f["objects"].as_array().unwrap().iter().take(2).enumerate() {
        let source = t.0.join(format!("source-{i}"));
        fs::write(&source, payload(row)).unwrap();
        cache.store(&key(row), &source).unwrap();
        out.push((key(row), payload(row)));
    }
    out
}
fn required(items: &[(ArtifactKey, Vec<u8>)]) -> BTreeMap<String, ArtifactKey> {
    items
        .iter()
        .enumerate()
        .map(|(i, (k, _))| (format!("model/part-{i}.bin"), k.clone()))
        .collect()
}
fn object_path(t: &Temp, k: &ArtifactKey) -> PathBuf {
    t.0.join("cache/objects").join(&k.object_id().unwrap()[7..])
}

#[test]
fn closed_and_reopened_cache_serves_every_required_file_with_held_handles() {
    let t = Temp(test_temp::directory("offline-start"));
    let items = populated(&t);
    // A new process-equivalent: nothing but the on-disk cache and the declared keys.
    let cache = open(&t.0.join("cache"));
    let before: Vec<_> = items
        .iter()
        .map(|(k, _)| fs::metadata(object_path(&t, k)).unwrap())
        .collect();
    let held = cache.resolve_offline(&required(&items)).unwrap();
    assert_eq!(held.len(), 2);
    for ((path, mut file), (_, bytes)) in held.into_iter().zip(&items) {
        let mut got = Vec::new();
        file.file.read_to_end(&mut got).unwrap();
        assert_eq!(&got, bytes, "{path}");
    }
    for ((k, _), b) in items.iter().zip(before) {
        let a = fs::metadata(object_path(&t, k)).unwrap();
        assert_eq!(
            (a.len(), a.modified().unwrap()),
            (b.len(), b.modified().unwrap())
        );
    }
}

#[test]
fn unavailable_files_are_named_with_causes_and_nothing_is_returned() {
    let t = Temp(test_temp::directory("offline-refuse"));
    let items = populated(&t);
    let f = fixture();
    let cache = open(&t.0.join("cache"));
    let mut req = required(&items);
    // Missing: a real key that was never stored.
    let absent = key(&f["objects"].as_array().unwrap()[2]);
    req.insert("model/absent.bin".into(), absent);
    // Foreign: same object identity position, different tensor identity claim.
    let mut foreign = items[0].0.clone();
    foreign.tensor_digest = format!("sha256:{}", "0".repeat(64));
    req.insert("model/foreign.bin".into(), foreign);
    // Corrupt: flip one byte of an indexed object (same length).
    let target = object_path(&t, &items[1].0);
    let mut perms = fs::metadata(&target).unwrap().permissions();
    perms.set_mode(0o600);
    fs::set_permissions(&target, perms).unwrap();
    let mut bytes = fs::read(&target).unwrap();
    bytes[0] ^= 1;
    fs::write(&target, &bytes).unwrap();
    let refusal = cache.resolve_offline(&req).unwrap_err();
    let got: Vec<(&str, &str)> = refusal
        .unavailable
        .iter()
        .map(|u| (u.path.as_str(), u.code))
        .collect();
    assert_eq!(
        got,
        vec![
            ("model/absent.bin", "artifact_missing"),
            ("model/foreign.bin", "artifact_missing"),
            ("model/part-1.bin", "artifact_corrupt"),
        ]
    );
    assert!(
        refusal
            .to_string()
            .starts_with("offline_startup_refused: model/absent.bin=artifact_missing")
    );
    // No repair, no eviction, no network fallback: the corrupt bytes are still what they were.
    assert_eq!(fs::read(&target).unwrap(), bytes);
    // Truncation is corruption too, and the healthy file alone still resolves.
    fs::write(&target, &bytes[..bytes.len() - 1]).unwrap();
    let only_first: BTreeMap<_, _> = required(&items).into_iter().take(1).collect();
    assert!(cache.resolve_offline(&only_first).is_ok());
    let both = cache.resolve_offline(&required(&items)).unwrap_err();
    assert_eq!(
        both.unavailable,
        vec![Unavailable {
            path: "model/part-1.bin".into(),
            code: "artifact_corrupt"
        }]
    );
}

#[test]
fn an_empty_or_unpopulated_cache_refuses_specifically_and_empty_requirements_are_invalid() {
    let t = Temp(test_temp::directory("offline-empty"));
    let f = fixture();
    let cache = open(&t.0.join("cache"));
    let k = key(&f["objects"].as_array().unwrap()[0]);
    let req = BTreeMap::from([("w.bin".to_owned(), k)]);
    assert_eq!(
        cache.resolve_offline(&req).unwrap_err().unavailable,
        vec![Unavailable {
            path: "w.bin".into(),
            code: "artifact_missing"
        }]
    );
    assert_eq!(
        cache
            .resolve_offline(&BTreeMap::new())
            .unwrap_err()
            .unavailable[0]
            .code,
        "artifact_assignment_invalid"
    );
}

#[test]
fn artifact_crate_has_no_network_capability() {
    let manifest = include_str!("../Cargo.toml");
    for banned in [
        "reqwest", "hyper", "ureq", "curl", "tokio", "iroh", "quinn", "rustls",
    ] {
        assert!(!manifest.contains(banned), "{banned} in artifacts manifest");
    }
    for entry in fs::read_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/src")).unwrap() {
        let path = entry.unwrap().path();
        let text = fs::read_to_string(&path).unwrap();
        for banned in ["std::net", "TcpStream", "UdpSocket", "TcpListener"] {
            assert!(!text.contains(banned), "{banned} in {}", path.display());
        }
    }
}

/// Whole startup path with the oracle's recorded expectation: populate, delete every source,
/// reopen, resolve offline, then stage the assignment. The staged report is the recorded
/// Python `materialize_assignment` output for the same keys.
#[test]
fn offline_resolution_then_staging_reproduces_the_recorded_oracle_report_without_sources() {
    let t = Temp(test_temp::directory("offline-stage"));
    let f = fixture();
    let cache = open(&t.0.join("cache"));
    for (i, row) in f["objects"].as_array().unwrap().iter().enumerate() {
        let source = t.0.join(format!("source-{i}"));
        fs::write(&source, payload(row)).unwrap();
        cache.store(&key(row), &source).unwrap();
        fs::remove_file(&source).unwrap();
    }
    drop(cache);
    let cache = open(&t.0.join("cache"));
    let row = &f["materializations"][0];
    let id = row["assignment_id"].as_str().unwrap();
    let required: BTreeMap<String, ArtifactKey> =
        serde_json::from_value(row["required"].clone()).unwrap();
    let held = cache.resolve_offline(&required).unwrap();
    assert_eq!(
        held.iter().map(|(p, _)| p.as_str()).collect::<Vec<_>>(),
        required.keys().map(String::as_str).collect::<Vec<_>>()
    );
    drop(held);
    let report = cache
        .materialize_assignment(id, &required, &t.0.join(id))
        .unwrap();
    assert_eq!(report, row["expected"]);
    assert_eq!(
        report["route_ready"], false,
        "cached bytes never grant route readiness"
    );
}

/// v0.1.4's offline start (`materialize_assignment`) stages an object that has a second name:
/// it never creates one, but a user can (backup tool, `ln`). The read-only offline path serves
/// it too, still hash-verified, and mutates nothing (the extra name, mode and index stay).
#[test]
fn hardlinked_object_is_served_like_v014_and_nothing_is_mutated() {
    let t = Temp(test_temp::directory("offline-hardlink"));
    let items = populated(&t);
    let cache = open(&t.0.join("cache"));
    let linked = object_path(&t, &items[0].0);
    let other = t.0.join("backup-name");
    fs::hard_link(&linked, &other).unwrap();
    let index = fs::read(t.0.join("cache/index.v1.json")).unwrap();
    let before = fs::metadata(&linked).unwrap();
    let mut held = cache.resolve_offline(&required(&items)).unwrap();
    assert_eq!(held.len(), 2);
    for ((path, file), (_, bytes)) in held.iter_mut().zip(&items) {
        let mut got = Vec::new();
        file.file.read_to_end(&mut got).unwrap();
        assert_eq!(&got, bytes, "{path}");
    }
    let after = fs::metadata(&linked).unwrap();
    assert_eq!(
        (after.nlink(), after.permissions().mode(), after.ino()),
        (2, before.permissions().mode(), before.ino())
    );
    assert_eq!(fs::read(&other).unwrap(), items[0].1);
    assert_eq!(fs::read(t.0.join("cache/index.v1.json")).unwrap(), index);
    assert_eq!(
        fs::read_dir(t.0.join("cache/quarantine")).unwrap().count(),
        0
    );
}

/// The relaxed link count never relaxes integrity: bytes changed through the other name, a
/// truncation, and a symlink at the object path are still refused with their specific cause.
#[test]
fn hardlinked_object_is_still_hash_and_type_checked() {
    let t = Temp(test_temp::directory("offline-hardlink-damage"));
    let items = populated(&t);
    let cache = open(&t.0.join("cache"));
    let linked = object_path(&t, &items[0].0);
    let other = t.0.join("backup-name");
    fs::hard_link(&linked, &other).unwrap();
    let code = |c: &Cache| {
        c.resolve_offline(&required(&items))
            .unwrap_err()
            .unavailable
            .iter()
            .map(|u| (u.path.clone(), u.code))
            .collect::<Vec<_>>()
    };
    let mut bytes = fs::read(&other).unwrap();
    fs::set_permissions(&other, fs::Permissions::from_mode(0o600)).unwrap();
    bytes[0] ^= 1;
    fs::write(&other, &bytes).unwrap();
    assert_eq!(
        code(&cache),
        vec![("model/part-0.bin".to_owned(), "artifact_corrupt")]
    );
    fs::write(&other, &bytes[..bytes.len() - 1]).unwrap();
    assert_eq!(
        code(&cache),
        vec![("model/part-0.bin".to_owned(), "artifact_corrupt")]
    );
    fs::remove_file(&linked).unwrap();
    std::os::unix::fs::symlink(&other, &linked).unwrap();
    assert_eq!(
        code(&cache),
        vec![("model/part-0.bin".to_owned(), "artifact_unsafe")]
    );
}
