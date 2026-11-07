// SPDX-License-Identifier: AGPL-3.0-only
#[path = "support/temp.rs"]
mod test_temp;
use artifacts::{ArtifactKey, Cache};
use budgets::Bytes;
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs, io::Read, os::unix::fs::PermissionsExt, path::PathBuf};
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = test_temp::directory("materialize");
        Self(p)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn cache(t: &Temp) -> Cache {
    Cache::open(
        &t.0.join("cache"),
        Bytes::new(10000),
        Bytes::new(65536),
        Bytes::new(4096),
    )
    .unwrap()
}
fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/materialization.json")).unwrap()
}
fn populate(t: &Temp, c: &Cache, f: &Value) {
    for (i, row) in f["objects"].as_array().unwrap().iter().enumerate() {
        let key: ArtifactKey = serde_json::from_value(row["key"].clone()).unwrap();
        let hex = row["payload_hex"].as_str().unwrap();
        let bytes: Vec<u8> = (0..hex.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
            .collect();
        let source = t.0.join(format!("source-{i}"));
        fs::write(&source, bytes).unwrap();
        c.store(&key, &source).unwrap();
    }
}
fn required(row: &Value) -> BTreeMap<String, ArtifactKey> {
    serde_json::from_value(row["required"].clone()).unwrap()
}
#[test]
fn recorded_assignment_manifest_pins_shared_objects_and_offline_reopen() {
    let t = Temp::new();
    let c = cache(&t);
    let f = fixture();
    populate(&t, &c, &f);
    for row in f["materializations"].as_array().unwrap() {
        let id = row["assignment_id"].as_str().unwrap();
        let dest = t.0.join(id);
        let r = required(row);
        assert_eq!(
            c.materialize_assignment(id, &r, &dest).unwrap(),
            row["expected"]
        );
        assert_eq!(fs::read_dir(&dest).unwrap().count(), r.len() + 1);
        let mut held = c.verify_materialization(id, &r, &dest).unwrap();
        for (name, file) in &mut held {
            let mut bytes = Vec::new();
            file.file.read_to_end(&mut bytes).unwrap();
            assert_eq!(bytes.len() as u64, r[name].size_bytes);
        }
    }
    assert_eq!(c.status().unwrap(), f["status"]);
    drop(c);
    let c = cache(&t);
    let row = &f["materializations"][0];
    let r = required(row);
    let dest = t.0.join("assignment-a");
    c.verify_materialization("assignment-a", &r, &dest).unwrap();
    c.retire_materialization("assignment-a", &r, &dest).unwrap();
    assert!(!dest.exists());
    assert_eq!(c.status().unwrap(), f["after_unpin"]);
    for row in f["objects"].as_array().unwrap() {
        c.verify(&serde_json::from_value(row["key"].clone()).unwrap())
            .unwrap();
    }
}
#[test]
fn staging_never_publishes_partial_missing_or_foreign_objects() {
    let t = Temp::new();
    let c = cache(&t);
    let f = fixture();
    populate(&t, &c, &f);
    let row = &f["materializations"][0];
    let mut r = required(row);
    r.values_mut().next().unwrap().model_revision = "foreign".into();
    let before = c.status().unwrap();
    let dest = t.0.join("missing");
    assert_eq!(
        c.materialize_assignment("assignment-a", &r, &dest)
            .unwrap_err()
            .code,
        "artifact_missing"
    );
    assert!(!dest.exists());
    assert_eq!(c.status().unwrap(), before);
    assert!(!fs::read_dir(&t.0).unwrap().any(|e| {
        e.unwrap()
            .file_name()
            .to_string_lossy()
            .ends_with(".partial")
    }));
    let r = required(row);
    fs::create_dir(&dest).unwrap();
    fs::write(dest.join("foreign"), b"keep").unwrap();
    assert_eq!(
        c.materialize_assignment("assignment-a", &r, &dest)
            .unwrap_err()
            .code,
        "artifact_destination_exists"
    );
    assert_eq!(fs::read(dest.join("foreign")).unwrap(), b"keep");
}
#[test]
fn manifest_digest_file_corruption_and_extra_files_cannot_be_retired_as_clean() {
    let t = Temp::new();
    let c = cache(&t);
    let f = fixture();
    populate(&t, &c, &f);
    let row = &f["materializations"][0];
    let r = required(row);
    let dest = t.0.join("assignment");
    c.materialize_assignment("assignment-a", &r, &dest).unwrap();
    let before = c.status().unwrap();
    let manifest = dest.join("assignment-materialization.json");
    let original = fs::read(&manifest).unwrap();
    fs::set_permissions(&manifest, fs::Permissions::from_mode(0o600)).unwrap();
    let mut changed: Value = serde_json::from_slice(&original).unwrap();
    changed["objects"][0]["tensor_digest"] = json!(format!("sha256:{}", "0".repeat(64)));
    fs::write(&manifest, serde_json::to_vec(&changed).unwrap()).unwrap();
    assert_eq!(
        c.verify_materialization("assignment-a", &r, &dest)
            .unwrap_err()
            .code,
        "artifact_manifest_mismatch"
    );
    assert!(c.retire_materialization("assignment-a", &r, &dest).is_err());
    assert_eq!(c.status().unwrap(), before);
    fs::write(&manifest, &original).unwrap();
    let first = dest.join(r.keys().next().unwrap());
    let clean = fs::read(&first).unwrap();
    fs::set_permissions(&first, fs::Permissions::from_mode(0o600)).unwrap();
    fs::write(&first, b"partial").unwrap();
    assert_eq!(
        c.verify_materialization("assignment-a", &r, &dest)
            .unwrap_err()
            .code,
        "artifact_corrupt"
    );
    let mut same_size = clean.clone();
    same_size[0] ^= 1;
    fs::write(&first, same_size).unwrap();
    assert_eq!(
        c.verify_materialization("assignment-a", &r, &dest)
            .unwrap_err()
            .code,
        "artifact_corrupt"
    );
    fs::write(&first, clean).unwrap();
    fs::write(dest.join("foreign"), b"keep").unwrap();
    assert_eq!(
        c.retire_materialization("assignment-a", &r, &dest)
            .unwrap_err()
            .code,
        "artifact_staging_foreign"
    );
    assert_eq!(fs::read(dest.join("foreign")).unwrap(), b"keep");
    assert_eq!(c.status().unwrap(), before);
    fs::remove_file(dest.join("foreign")).unwrap();
    c.retire_materialization("assignment-a", &r, &dest).unwrap();
}
#[test]
fn index_commit_failure_removes_only_owned_staging_and_keeps_cache_unchanged() {
    let t = Temp::new();
    let c = cache(&t);
    let f = fixture();
    populate(&t, &c, &f);
    let path = t.0.join("cache/index.v1.json");
    let original = fs::read(&path).unwrap();
    drop(c);
    let c = Cache::open(
        &t.0.join("cache"),
        Bytes::new(10000),
        Bytes::new(original.len() as u64),
        Bytes::new(4096),
    )
    .unwrap();
    let r = required(&f["materializations"][0]);
    let dest = t.0.join("not-published");
    assert_eq!(
        c.materialize_assignment("assignment-a", &r, &dest)
            .unwrap_err()
            .code,
        "artifact_index_too_large"
    );
    assert_eq!(fs::read(&path).unwrap(), original);
    assert!(!dest.exists());
    assert_eq!(c.status().unwrap()["pinned_object_count"], 0);
    assert!(!fs::read_dir(&t.0).unwrap().any(|e| {
        e.unwrap()
            .file_name()
            .to_string_lossy()
            .ends_with(".partial")
    }));
}
#[test]
fn nested_staging_is_verified_and_retired_without_following_symlinks() {
    use std::os::unix::fs::symlink;
    let t = Temp::new();
    let c = cache(&t);
    let f = fixture();
    populate(&t, &c, &f);
    let r: BTreeMap<String, ArtifactKey> = required(&f["materializations"][0])
        .into_iter()
        .map(|(name, key)| (format!("model/nested/{name}"), key))
        .collect();
    let dest = t.0.join("nested");
    c.materialize_assignment("nested", &r, &dest).unwrap();
    c.verify_materialization("nested", &r, &dest).unwrap();
    let original = dest.join("model");
    let saved = t.0.join("saved");
    fs::rename(&original, &saved).unwrap();
    symlink(&saved, &original).unwrap();
    assert_eq!(
        c.retire_materialization("nested", &r, &dest)
            .unwrap_err()
            .code,
        "artifact_staging_foreign"
    );
    assert!(saved.join("nested").exists());
    fs::remove_file(&original).unwrap();
    fs::rename(&saved, &original).unwrap();
    c.retire_materialization("nested", &r, &dest).unwrap();
    assert!(!dest.exists());
}
#[test]
fn traversal_manifest_collision_and_symlink_destination_refuse() {
    use std::os::unix::fs::symlink;
    let t = Temp::new();
    let c = cache(&t);
    let f = fixture();
    populate(&t, &c, &f);
    let r = required(&f["materializations"][0]);
    let key = r.values().next().unwrap().clone();
    for path in [
        "../escape",
        "/absolute",
        "a/../escape",
        "a//b",
        "assignment-materialization.json",
    ] {
        let req = BTreeMap::from([(path.into(), key.clone())]);
        assert_eq!(
            c.materialize_assignment("a", &req, &t.0.join("unsafe"))
                .unwrap_err()
                .code,
            "artifact_path_invalid"
        );
        assert!(!t.0.join("unsafe").exists());
    }
    let dest = t.0.join("alias");
    symlink(t.0.join("missing"), &dest).unwrap();
    assert_eq!(
        c.materialize_assignment("a", &r, &dest).unwrap_err().code,
        "artifact_destination_exists"
    );
}
