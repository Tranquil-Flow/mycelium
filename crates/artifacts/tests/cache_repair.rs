// SPDX-License-Identifier: AGPL-3.0-only
//! Round 7: the artifact cache against the actual v0.1.4 `AssignmentArtifactCache`.
//!
//! `fixtures/cache-repair.json` records, for scripted scenarios (truncated, appended, flipped,
//! emptied, deleted, directory/dangling-link/foreign objects, eviction, pins, materialization
//! errors), the oracle's outcome after every step and the complete resulting on-disk state of the
//! cache root and of each destination. This test replays the same steps with the Rust cache and
//! compares outcome, files left on disk (path, kind, size, SHA-256, mode, normalised index) and
//! the error text mapped to the Rust error code.
#[path = "support/temp.rs"]
mod test_temp;
use artifacts::{ArtifactKey, Cache};
use budgets::Bytes;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
};

const REVISION: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const MANIFEST: &str = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/cache-repair.json")).unwrap()
}

/// Round 8: the same replay for the on-disk states a real v0.1.4 install can produce (directory
/// modes, symlinked root parents, hard links, hand-edited index entries).
fn compat_fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/cache-compat.json")).unwrap()
}

fn content(spec: &Value) -> Vec<u8> {
    vec![spec[0].as_u64().unwrap() as u8; spec[1].as_u64().unwrap() as usize]
}

fn key_for(spec: &Value, digest: Option<&str>) -> ArtifactKey {
    let data = content(spec);
    ArtifactKey {
        model_revision: REVISION.into(),
        manifest_digest: MANIFEST.into(),
        format: "safetensors".into(),
        quantization: "int8-weight-only".into(),
        tensor_digest: digest
            .map(str::to_owned)
            .unwrap_or_else(|| format!("sha256:{:x}", Sha256::digest(&data))),
        size_bytes: data.len() as u64,
    }
}

fn hex(bytes: &str) -> Vec<u8> {
    (0..bytes.len() / 2)
        .map(|i| u8::from_str_radix(&bytes[i * 2..i * 2 + 2], 16).unwrap())
        .collect()
}

/// Oracle error text/class → the Rust error codes that mean the same refusal.
fn allowed_codes(class: &str, message: &str) -> &'static [&'static str] {
    match (class, message) {
        ("ValueError", "cache source does not match object key") => {
            &["artifact_corrupt", "artifact_key_invalid"]
        }
        ("ValueError", "assignment cache object is missing or corrupt") => &[
            "artifact_missing",
            "artifact_corrupt",
            "artifact_foreign",
            "artifact_unsafe",
        ],
        ("ValueError", "assignment destination must not exist") => &["artifact_destination_exists"],
        ("ValueError", "assignment_id must be non-empty")
        | ("ValueError", "assignment requires at least one object") => {
            &["artifact_assignment_invalid"]
        }
        ("ValueError", "assignment artifact path is unsafe") => &["artifact_path_invalid"],
        ("ValueError", "cache index is invalid") => &["artifact_index_invalid"],
        ("ValueError", "cache index is unsafe") => &["artifact_unsafe"],
        ("ValueError", "cache root may not be a symlink") => &["artifact_unsafe"],
        ("FileExistsError", _) => &["artifact_unsafe", "artifact_directory_failed"],
        ("AttributeError", _) => &["cache_not_open"],
        ("TypeError", _) | ("KeyError", _) => &["artifact_index_invalid"],
        ("FileNotFoundError", _) => &["artifact_missing"],
        other => panic!("unmapped oracle error {other:?}"),
    }
}

struct Run {
    ws: PathBuf,
    cache_path: PathBuf,
    cache: Option<Cache>,
    objects: Map<String, Value>,
    dests: Vec<String>,
    /// For the last `materialize` step: did the read-only offline startup path
    /// (`resolve_offline`, run just before, same files) serve the whole set?
    offline_probe: Option<bool>,
}

impl Run {
    fn object_path(&self, name: &str) -> PathBuf {
        let key = key_for(&self.objects[name], None);
        self.cache_path
            .join("objects")
            .join(&key.object_id().unwrap()[7..])
    }
    fn source(&self, name: &str) -> PathBuf {
        let path = self.ws.join("src").join(name);
        if !path.exists() {
            fs::write(&path, content(&self.objects[name])).unwrap();
        }
        path
    }
    fn c(&self) -> Result<&Cache, String> {
        self.cache
            .as_ref()
            .ok_or_else(|| "cache_not_open".to_owned())
    }
    fn open(&mut self, step: &Value) -> Result<Value, String> {
        // A refused (re)open leaves the previous handle (and root) in place, as the oracle's
        // variables are.
        let path = step["root"]
            .as_str()
            .map_or_else(|| self.cache_path.clone(), |r| self.ws.join(r));
        match Cache::open(
            &path,
            Bytes::new(step["max_bytes"].as_u64().unwrap()),
            Bytes::new(8 * 1024 * 1024),
            Bytes::new(1024 * 1024),
        ) {
            Ok(c) => {
                self.cache = Some(c);
                self.cache_path = path;
                Ok(Value::Null)
            }
            Err(e) => Err(e.code.to_owned()),
        }
    }
    fn damage(&self, step: &Value) {
        let path = self.object_path(step["obj"].as_str().unwrap());
        let kind = step["kind"].as_str().unwrap();
        let chmod =
            |mode: u32| fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
        if kind != "delete" && path.exists() && !path.is_symlink() && !path.is_dir() {
            chmod(0o600);
        }
        match kind {
            "truncate" => {
                let data = fs::read(&path).unwrap();
                fs::write(&path, &data[..data.len() / 2]).unwrap();
            }
            "append" => {
                let mut data = fs::read(&path).unwrap();
                data.push(b'x');
                fs::write(&path, data).unwrap();
            }
            "flip" => {
                let mut data = fs::read(&path).unwrap();
                data[0] ^= 0xFF;
                fs::write(&path, data).unwrap();
            }
            "empty" => fs::write(&path, b"").unwrap(),
            "delete" => {
                chmod(0o600);
                fs::remove_file(&path).unwrap();
            }
            "directory" => {
                chmod(0o600);
                fs::remove_file(&path).unwrap();
                fs::create_dir(&path).unwrap();
            }
            "symlink_to" => {
                let other = self.object_path(step["other"].as_str().unwrap());
                chmod(0o600);
                fs::remove_file(&path).unwrap();
                symlink(other, &path).unwrap();
            }
            "dangling_symlink" => {
                chmod(0o600);
                fs::remove_file(&path).unwrap();
                symlink(self.cache_path.join("objects/nowhere"), &path).unwrap();
            }
            "hardlink" => fs::hard_link(&path, self.ws.join("src/hardlink-of-object")).unwrap(),
            "chmod_writable" => chmod(0o666),
            "place_foreign_at_target" => {
                fs::write(&path, hex(step["hex"].as_str().unwrap())).unwrap()
            }
            other => panic!("unknown damage {other}"),
        }
    }
    fn index_drop(&self, name: &str) {
        let path = self.cache_path.join("index.v1.json");
        let mut doc: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        let id = key_for(&self.objects[name], None).object_id().unwrap();
        doc["entries"].as_object_mut().unwrap().remove(&id);
        let mut bytes = serde_json::to_vec(&doc).unwrap();
        bytes.push(b'\n');
        fs::write(&path, bytes).unwrap();
    }
    /// Hand edits of the index, the same ones the oracle script applies (states v0.1.4 never writes).
    fn index_edit(&self, name: &str, kind: &str) {
        let path = self.cache_path.join("index.v1.json");
        let mut doc: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        let id = key_for(&self.objects[name], None).object_id().unwrap();
        let entries = doc["entries"].as_object_mut().unwrap();
        match kind {
            "relative_path_bogus" => entries[&id]["relative_path"] = json!("../bogus"),
            "key_changed" => entries[&id]["key"]["model_revision"] = json!("someone-else"),
            "pins_removed" => {
                entries[&id].as_object_mut().unwrap().remove("pins");
            }
            "extra_field" => entries[&id]["note"] = json!("hand-added"),
            "moved_id" => {
                let entry = entries.remove(&id).unwrap();
                entries.insert(format!("sha256:{}", "0".repeat(64)), entry);
            }
            "size_huge" => entries[&id]["size_bytes"] = json!(1_000_000),
            "traversal_id" => {
                let mut entry = entries.remove(&id).unwrap();
                entry["size_bytes"] = json!(1_000_000);
                entries.insert("sha256:../../.canary".into(), entry);
            }
            "size_string" => entries[&id]["size_bytes"] = json!("100"),
            "last_access_missing" => {
                entries[&id]
                    .as_object_mut()
                    .unwrap()
                    .remove("last_access_unix_ns");
            }
            other => panic!("unknown edit {other}"),
        }
        let mut bytes = serde_json::to_vec(&doc).unwrap();
        bytes.push(b'\n');
        fs::write(&path, bytes).unwrap();
    }
    fn step(&mut self, step: &Value) -> Result<Value, String> {
        let op = step["op"].as_str().unwrap();
        match op {
            "open" | "reopen" => self.open(step),
            "store" | "store_missing_source" => {
                let name = step["obj"].as_str().unwrap();
                let key = key_for(&self.objects[name], step["digest"].as_str());
                let source = if let Some(rel) = step["source_rel"].as_str() {
                    self.ws.join(rel)
                } else if op == "store" {
                    self.source(step["source"].as_str().unwrap_or(name))
                } else {
                    self.ws.join("src/does-not-exist")
                };
                self.c()?
                    .store(&key, &source)
                    .map(|o| json!(o.as_str()))
                    .map_err(|e| e.code.to_owned())
            }
            "damage" => {
                self.damage(step);
                Ok(Value::Null)
            }
            "foreign" => {
                fs::write(
                    self.cache_path
                        .join("objects")
                        .join(step["name"].as_str().unwrap()),
                    hex(step["hex"].as_str().unwrap()),
                )
                .unwrap();
                Ok(Value::Null)
            }
            "index_drop" => {
                self.index_drop(step["obj"].as_str().unwrap());
                Ok(Value::Null)
            }
            "materialize" => {
                let dest_name = step["dest"].as_str().unwrap();
                self.dests.push(dest_name.to_owned());
                let required: BTreeMap<String, ArtifactKey> = step["files"]
                    .as_object()
                    .unwrap()
                    .iter()
                    .map(|(p, o)| (p.clone(), key_for(&self.objects[o.as_str().unwrap()], None)))
                    .collect();
                self.offline_probe = Some(self.c()?.resolve_offline(&required).is_ok());
                self.c()?
                    .materialize_assignment(
                        step["assignment"].as_str().unwrap(),
                        &required,
                        &self.ws.join(dest_name),
                    )
                    .map_err(|e| e.code.to_owned())
            }
            "evict" => self
                .c()?
                .evict()
                .map(|ids| json!(ids))
                .map_err(|e| e.code.to_owned()),
            "status" => self.c()?.status().map_err(|e| e.code.to_owned()),
            "unpin" => self
                .c()?
                .unpin_assignment(step["assignment"].as_str().unwrap())
                .map(|()| Value::Null)
                .map_err(|e| e.code.to_owned()),
            "mkdir" => {
                let path = self.ws.join(step["path"].as_str().unwrap());
                fs::create_dir_all(&path).unwrap();
                let mode = step["mode"].as_u64().unwrap() as u32;
                fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
                Ok(Value::Null)
            }
            "chmod" => {
                let path = self.ws.join(step["path"].as_str().unwrap());
                let mode = step["mode"].as_u64().unwrap() as u32;
                fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
                Ok(Value::Null)
            }
            "symlink" => {
                symlink(
                    self.ws.join(step["target"].as_str().unwrap()),
                    self.ws.join(step["path"].as_str().unwrap()),
                )
                .unwrap();
                Ok(Value::Null)
            }
            "hardlink" => {
                fs::hard_link(
                    self.ws.join(step["target"].as_str().unwrap()),
                    self.ws.join(step["path"].as_str().unwrap()),
                )
                .unwrap();
                Ok(Value::Null)
            }
            "canary" => {
                fs::write(self.ws.join(".canary"), b"canary").unwrap();
                Ok(Value::Null)
            }
            "write_file" => {
                fs::write(
                    self.ws.join(step["path"].as_str().unwrap()),
                    b"not a directory",
                )
                .unwrap();
                Ok(Value::Null)
            }
            "write_source" => {
                let name = step["name"].as_str().unwrap();
                fs::write(
                    self.ws.join("src").join(name),
                    content(&self.objects[step["obj"].as_str().unwrap()]),
                )
                .unwrap();
                Ok(Value::Null)
            }
            "swap_objects_for_symlink" => {
                let objects = self.cache_path.join("objects");
                let elsewhere = self.ws.join("elsewhere");
                for entry in fs::read_dir(&objects).unwrap() {
                    let entry = entry.unwrap();
                    fs::rename(entry.path(), elsewhere.join(entry.file_name())).unwrap();
                }
                fs::remove_dir(&objects).unwrap();
                symlink(&elsewhere, &objects).unwrap();
                Ok(Value::Null)
            }
            "swap_index_for_symlink" => {
                let index = self.cache_path.join("index.v1.json");
                let elsewhere = self.ws.join("index-elsewhere.json");
                fs::rename(&index, &elsewhere).unwrap();
                symlink(&elsewhere, &index).unwrap();
                Ok(Value::Null)
            }
            "swap_lock_for_symlink" => {
                let lock = self.cache_path.join(".cache.lock");
                let elsewhere = self.ws.join("lock-elsewhere");
                fs::remove_file(&lock).unwrap();
                fs::write(&elsewhere, b"").unwrap();
                symlink(&elsewhere, &lock).unwrap();
                Ok(Value::Null)
            }
            "index_edit" => {
                self.index_edit(
                    step["obj"].as_str().unwrap(),
                    step["kind"].as_str().unwrap(),
                );
                Ok(Value::Null)
            }
            "stat" => {
                let meta = fs::symlink_metadata(self.ws.join(step["path"].as_str().unwrap()))
                    .map_err(|e| format!("stat_failed:{:?}", e.kind()))?;
                let kind = if meta.file_type().is_symlink() {
                    "symlink"
                } else if meta.is_dir() {
                    "dir"
                } else {
                    "file"
                };
                Ok(json!({"kind": kind, "mode": meta.mode() & 0o7777, "nlink": meta.nlink()}))
            }
            other => panic!("unknown op {other}"),
        }
    }
}

fn normalise_name(rel: &str) -> String {
    // quarantine/<64 hex>.<ns> → quarantine/<64 hex>.T
    if let Some(rest) = rel.strip_prefix("quarantine/")
        && let Some((id, ns)) = rest.split_once('.')
        && id.len() == 64
        && !ns.is_empty()
        && ns.bytes().all(|b| b.is_ascii_digit())
    {
        return format!("quarantine/{id}.T");
    }
    rel.to_owned()
}

fn walk(ws: &Path, base: &Path, dir: &Path, rows: &mut BTreeMap<String, Value>) {
    // `os.walk` of a non-directory yields nothing.
    let Ok(listing) = fs::read_dir(dir) else {
        return;
    };
    for entry in listing {
        let path = entry.unwrap().path();
        let rel = normalise_name(path.strip_prefix(base).unwrap().to_str().unwrap());
        let meta = fs::symlink_metadata(&path).unwrap();
        let mode = meta.mode() & 0o7777;
        if meta.file_type().is_symlink() {
            rows.insert(
                rel,
                json!({"kind":"symlink","target":fs::read_link(&path).unwrap().to_str().unwrap().replace(ws.to_str().unwrap(), "$WS")}),
            );
        } else if meta.is_dir() {
            rows.insert(rel, json!({"kind":"dir","mode":mode}));
            walk(ws, base, &path, rows);
        } else {
            let data = fs::read(&path).unwrap();
            let mut row = json!({"kind":"file","size":data.len(),"mode":mode});
            if rel == "index.v1.json" {
                let mut doc: Value = serde_json::from_slice(&data).unwrap();
                for e in doc["entries"].as_object_mut().unwrap().values_mut() {
                    // A hand-edited entry may lack the field; only numbers are normalised.
                    if e["last_access_unix_ns"].is_u64() {
                        e["last_access_unix_ns"] = if e["last_access_unix_ns"] == 0 {
                            json!(0)
                        } else {
                            json!("t")
                        };
                    }
                }
                row["index"] = doc;
            } else {
                row["sha256"] = json!(format!("{:x}", Sha256::digest(&data)));
            }
            rows.insert(rel, row);
        }
    }
}

fn state(run: &Run) -> Value {
    let mut rows = BTreeMap::new();
    let mut inner = BTreeMap::new();
    walk(&run.ws, &run.cache_path, &run.cache_path, &mut inner);
    rows.extend(inner.into_iter().map(|(k, v)| (format!("cache/{k}"), v)));
    for name in &run.dests {
        let dest = run.ws.join(name);
        if fs::symlink_metadata(&dest).is_ok() {
            rows.insert(format!("{name}/"), json!({"kind":"dir"}));
            let mut inner = BTreeMap::new();
            walk(&run.ws, &dest, &dest, &mut inner);
            rows.extend(inner.into_iter().map(|(k, v)| (format!("{name}/{k}"), v)));
        }
    }
    for entry in fs::read_dir(&run.ws).unwrap() {
        let name = entry.unwrap().file_name().into_string().unwrap();
        if name.starts_with('.') {
            rows.insert(name, json!({"kind":"stray"}));
        }
    }
    json!(rows)
}

fn diff_state(want: &Value, have: &Value) -> String {
    let (w, h) = (want.as_object().unwrap(), have.as_object().unwrap());
    let mut out = Vec::new();
    for (k, v) in w {
        match h.get(k) {
            None => out.push(format!("missing in Rust: {k} = {v}")),
            Some(x) if x != v => out.push(format!("differs {k}: oracle {v} / rust {x}")),
            _ => {}
        }
    }
    for (k, v) in h {
        if !w.contains_key(k) {
            out.push(format!("extra in Rust: {k} = {v}"));
        }
    }
    out.join("\n  ")
}

/// Directories created without an explicit mode (missing parents, intermediate staging
/// directories) get `0o777 & !umask` on both sides; the oracle ran under umask 022 and recorded
/// 0o755. Directory link counts are not compared (they follow the entry count).
fn stat_matches(step: &Value, want: &Value, have: &Value) -> bool {
    let mut want_mode = want["mode"].as_u64().unwrap();
    if step["default_dir"] == true {
        assert_eq!(want_mode, 0o755, "oracle recorded under umask 022");
        want_mode = u64::from(default_directory_mode());
    }
    want["kind"] == have["kind"]
        && want_mode == have["mode"].as_u64().unwrap()
        && (want["kind"] != "file" || want["nlink"] == have["nlink"])
}

fn default_directory_mode() -> u32 {
    use std::os::unix::fs::DirBuilderExt;
    let probe = test_temp::directory("umask-probe");
    let inner = probe.join("probe");
    fs::DirBuilder::new().mode(0o777).create(&inner).unwrap();
    let mode = fs::metadata(&inner).unwrap().mode() & 0o7777;
    fs::remove_dir_all(probe).unwrap();
    mode
}

fn replay(scenario: &Value) -> Vec<String> {
    let ws = test_temp::directory("cache-repair");
    fs::create_dir(ws.join("src")).unwrap();
    let mut run = Run {
        cache_path: ws.join("cache"),
        ws: ws.clone(),
        cache: None,
        objects: scenario["objects"].as_object().unwrap().clone(),
        dests: Vec::new(),
        offline_probe: None,
    };
    let mut mismatches = Vec::new();
    let name = scenario["name"].as_str().unwrap();
    for (i, recorded) in scenario["steps"].as_array().unwrap().iter().enumerate() {
        let step = &recorded["step"];
        let oracle = &recorded["result"];
        let label = format!("{name} step {i} {step}");
        run.offline_probe = None;
        // A harness step that cannot be applied (the file it would damage is not there because
        // an earlier Rust step refused) is a mismatch, not an abort of the whole run.
        let got = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| run.step(step)))
            .unwrap_or_else(|_| Err("harness_step_failed".to_owned()));
        match (oracle["ok"] == true, got) {
            (true, Ok(value)) => {
                let want = &oracle["value"];
                // Wall-clock-free and path-free: compare as parsed JSON.
                let same = if step["op"] == "stat" {
                    stat_matches(step, want, &value)
                } else {
                    want == &value
                };
                if !same {
                    mismatches.push(format!("{label}: value oracle {want} / rust {value}"));
                }
            }
            (true, Err(code)) => {
                mismatches.push(format!("{label}: oracle accepted, Rust refused {code}"))
            }
            (false, Ok(value)) => mismatches.push(format!(
                "{label}: oracle raised {} {:?}; Rust accepted {value}",
                oracle["class"], oracle["message"]
            )),
            (false, Err(code)) => {
                let allowed = allowed_codes(
                    oracle["class"].as_str().unwrap(),
                    oracle["message"]
                        .as_str()
                        .unwrap()
                        .split(": ")
                        .next()
                        .unwrap_or(""),
                );
                if !allowed.contains(&code.as_str()) {
                    mismatches.push(format!(
                        "{label}: oracle {} {:?} ↔ Rust {code}, expected one of {allowed:?}",
                        oracle["class"], oracle["message"]
                    ));
                }
            }
        }
        // v0.1.4 starts from its cache through `materialize_assignment`, which hash-verifies every
        // object it stages. Whatever it serves, the read-only offline startup must serve too (no
        // hard failure where v0.1.4 works), and what it refuses as missing/corrupt must refuse.
        if let Some(served) = run.offline_probe {
            let refused_by_oracle = oracle["ok"] == false
                && oracle["message"] == "assignment cache object is missing or corrupt";
            if oracle["ok"] == true && !served {
                mismatches.push(format!(
                    "{label}: offline startup refused what the oracle materialized"
                ));
            }
            if refused_by_oracle && served {
                mismatches.push(format!(
                    "{label}: offline startup served what the oracle refused as missing or corrupt"
                ));
            }
        }
        if !recorded["state"].is_null() {
            let have = state(&run);
            if have != recorded["state"] {
                mismatches.push(format!(
                    "{label}: on-disk state differs\n  {}",
                    diff_state(&recorded["state"], &have)
                ));
            }
        }
    }
    run.cache = None;
    fs::remove_dir_all(ws).unwrap();
    mismatches
}

#[test]
fn provenance_pins_the_exact_fixture() {
    let raw = include_str!("fixtures/cache-repair.json");
    let p: Value =
        serde_json::from_str(include_str!("fixtures/cache-repair-provenance.json")).unwrap();
    assert_eq!(
        p["output_sha256"].as_str().unwrap(),
        format!("{:x}", Sha256::digest(raw.as_bytes()))
    );
    assert_eq!(
        p["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let f = fixture();
    let scenarios = f["scenarios"].as_array().unwrap();
    assert_eq!(p["counts"]["scenarios"], scenarios.len());
    let steps: usize = scenarios
        .iter()
        .map(|s| s["steps"].as_array().unwrap().len())
        .sum();
    assert_eq!(p["counts"]["steps"], steps);
}

/// Deliberate, documented differences from v0.1.4 that remain (each keeps a safety check for a
/// state v0.1.4 never writes, or refuses a case where v0.1.4's own report validator rejects its
/// output). Every mismatch of a listed scenario must contain one of its substrings, so a change
/// in either direction (or a new, unexpected mismatch) is visible; every other scenario must
/// match the oracle exactly. Evidence and rationale: `L7-PLANNER.md` round 8 (V03 rows).
const KNOWN_DIVERGENCES: &[(&str, &[&str])] = &[
    // Hard-linked object: v0.1.4 reuses it (`st_nlink >= 1`; it never creates a link, so this is
    // only a user-made state). Rust re-acquires it from the verified source: the outcome is
    // "repaired" and the cache converges (second store: reused, materialize works).
    (
        "hardlinked-object-still-reused",
        &[
            "value oracle \"reused\" / rust \"repaired\"",
            "on-disk state differs",
        ],
    ),
    (
        "hardlinked-object-converges-then-reuses",
        &[
            "value oracle \"reused\" / rust \"repaired\"",
            "on-disk state differs",
        ],
    ),
    // v0.1.4 materializes one object under two paths, writing unassigned_object_count = -1 into
    // a manifest that its own validate_materialization_report rejects (duplicate object ids).
    (
        "materialize-duplicate-object-under-two-paths",
        &[
            "oracle accepted, Rust refused artifact_assignment_invalid",
            "on-disk state differs",
            "materialize-duplicate-object-under-two-paths step 3",
        ],
    ),
    // Staging directories Rust creates are all private (0700); v0.1.4 leaves intermediate
    // directories of a nested path at the default mode.
    (
        "fresh-root-creates-missing-parents-default-mode",
        &[
            "differs st/n1/m: oracle",
            "value oracle {\"kind\":\"dir\",\"mode\":493",
        ],
    ),
    // Symlinks INSIDE the cache root are refused (integrator ruling 2); v0.1.4 follows them.
    (
        "symlinked-objects-directory-is-followed-by-v014",
        &[
            "oracle accepted, Rust refused artifact_unsafe",
            "on-disk state differs",
        ],
    ),
    (
        "symlinked-lock-file-is-followed-by-v014",
        &["oracle accepted, Rust refused artifact_lock_failed"],
    ),
    // The object Rust re-owns for staging is a fresh private 0400 inode; the harness left the
    // oracle's hard-linked object at 0600 (see `hardlinked_object_is_reowned_for_staging`).
    (
        "hardlinked-object-then-materialize-works",
        &["on-disk state differs"],
    ),
    // Index entries that would make v0.1.4 delete a file outside the cache or fail with a
    // TypeError/KeyError on its next mutation are refused when the index is read.
    (
        "edited-entry-traversal_id",
        &[
            "oracle accepted, Rust refused artifact_index_invalid",
            "on-disk state differs",
        ],
    ),
    (
        "edited-entry-size_string",
        &[
            "oracle accepted, Rust refused artifact_index_invalid",
            "on-disk state differs",
        ],
    ),
];

fn check_fixture(f: &Value, unexpected: &mut Vec<String>, seen: &mut Vec<String>) {
    for scenario in f["scenarios"].as_array().unwrap() {
        let name = scenario["name"].as_str().unwrap();
        let mismatches = replay(scenario);
        match KNOWN_DIVERGENCES.iter().find(|(n, _)| *n == name) {
            Some((_, expected)) => {
                assert!(
                    !mismatches.is_empty(),
                    "{name}: documented divergence no longer observed"
                );
                for m in &mismatches {
                    if !expected.iter().any(|e| m.contains(e)) {
                        unexpected.push(m.clone());
                    }
                }
                seen.push(name.to_owned());
            }
            None => unexpected.extend(mismatches),
        }
    }
}

#[test]
fn damaged_entries_are_repaired_evicted_and_left_on_disk_exactly_as_v014_does() {
    let mut unexpected = Vec::new();
    let mut seen = Vec::new();
    check_fixture(&fixture(), &mut unexpected, &mut seen);
    check_fixture(&compat_fixture(), &mut unexpected, &mut seen);
    assert_eq!(
        seen.len(),
        KNOWN_DIVERGENCES.len(),
        "every listed divergence must be observed: {seen:?}"
    );
    assert!(
        unexpected.is_empty(),
        "{} mismatches:\n{}",
        unexpected.len(),
        unexpected.join("\n")
    );
}

#[test]
fn compat_provenance_pins_the_exact_fixtures() {
    let p: Value =
        serde_json::from_str(include_str!("fixtures/cache-compat-provenance.json")).unwrap();
    for (name, raw) in [
        (
            "cache-compat.json",
            include_str!("fixtures/cache-compat.json"),
        ),
        (
            "index-corpus.json",
            include_str!("fixtures/index-corpus.json"),
        ),
    ] {
        assert_eq!(
            p["output_sha256"][name].as_str().unwrap(),
            format!("{:x}", Sha256::digest(raw.as_bytes())),
            "{name}"
        );
    }
    assert_eq!(
        p["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let f = compat_fixture();
    let scenarios = f["scenarios"].as_array().unwrap();
    assert_eq!(p["counts"]["scenarios"], scenarios.len());
    let steps: usize = scenarios
        .iter()
        .map(|s| s["steps"].as_array().unwrap().len())
        .sum();
    assert_eq!(p["counts"]["steps"], steps);
}

/// Rebuild, from the oracle's recorded complete state, the exact tree v0.1.4 left behind
/// (cache root, objects, index, and a nested staging destination whose intermediate directories
/// have the default mode under a parent created with the default mode) and prove this crate
/// verifies, retires and re-opens it: the in-place upgrade of a v0.1.4 install.
#[test]
fn a_staging_tree_written_by_v014_verifies_retires_and_reopens() {
    let f = compat_fixture();
    let scenario = f["scenarios"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["name"] == "fresh-root-creates-missing-parents-default-mode")
        .unwrap();
    let objects = scenario["objects"].as_object().unwrap();
    let steps = scenario["steps"].as_array().unwrap();
    let materialize = steps
        .iter()
        .find(|s| s["step"]["op"] == "materialize")
        .unwrap();
    let manifest = &materialize["result"]["value"];
    let state = steps.last().unwrap()["state"].as_object().unwrap();
    let by_sha: BTreeMap<String, Vec<u8>> = objects
        .values()
        .map(|spec| {
            (
                format!("{:x}", Sha256::digest(content(spec))),
                content(spec),
            )
        })
        .collect();
    let ws = test_temp::directory("v014-tree");
    // The oracle created `st` and `a/b` with the default mode and the cache root 0700.
    fs::create_dir_all(ws.join("st")).unwrap();
    let mut rows: Vec<(&String, &Value)> = state
        .iter()
        .filter(|(k, _)| k.starts_with("cache/") || k.starts_with("st/n1/"))
        .collect();
    rows.sort_by_key(|(k, _)| k.matches('/').count());
    fs::create_dir(ws.join("cache")).unwrap();
    fs::set_permissions(ws.join("cache"), fs::Permissions::from_mode(0o700)).unwrap();
    fs::create_dir(ws.join("st/n1")).unwrap();
    fs::set_permissions(ws.join("st/n1"), fs::Permissions::from_mode(0o700)).unwrap();
    for (rel, row) in &rows {
        if rel.ends_with('/') {
            continue;
        }
        let path = ws.join(rel);
        let mode = row["mode"].as_u64().unwrap_or(0o700) as u32;
        match row["kind"].as_str().unwrap() {
            "dir" => {
                fs::create_dir(&path).unwrap();
                fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
            }
            "file" => {
                let data = if rel.ends_with("index.v1.json") {
                    let mut doc = row["index"].clone();
                    for e in doc["entries"].as_object_mut().unwrap().values_mut() {
                        e["last_access_unix_ns"] = json!(1_700_000_000_000_000_000u64);
                    }
                    let mut bytes = serde_json::to_vec(&doc).unwrap();
                    bytes.push(b'\n');
                    bytes
                } else if rel.ends_with("assignment-materialization.json") {
                    let mut bytes = serde_json::to_vec(manifest).unwrap();
                    bytes.push(b'\n');
                    bytes
                } else if rel.ends_with(".cache.lock") {
                    Vec::new()
                } else {
                    by_sha[row["sha256"].as_str().unwrap()].clone()
                };
                fs::write(&path, data).unwrap();
                fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
            }
            other => panic!("unexpected {other}"),
        }
    }
    let required: BTreeMap<String, ArtifactKey> = steps
        .iter()
        .find(|s| s["step"]["op"] == "materialize")
        .unwrap()["step"]["files"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(p, o)| (p.clone(), key_for(&objects[o.as_str().unwrap()], None)))
        .collect();
    // The intermediate directory the oracle left at the default mode must be there.
    assert_eq!(
        fs::metadata(ws.join("st/n1/m")).unwrap().mode() & 0o7777,
        0o755
    );
    let cache = Cache::open(
        &ws.join("cache"),
        Bytes::new(1000),
        Bytes::new(8 * 1024 * 1024),
        Bytes::new(1024 * 1024),
    )
    .unwrap();
    // v0.1.4 writes the default mode, which follows the user's umask (0777, 0775, 0755, 0750 and
    // 0700 are all real): no mode of the parent or of an intermediate directory is a refusal.
    for mode in [0o777, 0o775, 0o755, 0o750, 0o700] {
        for dir in ["st", "st/n1/m"] {
            fs::set_permissions(ws.join(dir), fs::Permissions::from_mode(mode)).unwrap();
        }
        let held = cache
            .verify_materialization("n1", &required, &ws.join("st/n1"))
            .unwrap_or_else(|e| panic!("mode {mode:o}: {e}"));
        assert_eq!(held.len(), 2);
    }
    assert_eq!(cache.status().unwrap()["pinned_object_count"], 2);
    cache
        .retire_materialization("n1", &required, &ws.join("st/n1"))
        .unwrap();
    assert!(!ws.join("st/n1").exists());
    assert_eq!(cache.status().unwrap()["pinned_object_count"], 0);
    drop(cache);
    fs::remove_dir_all(ws).unwrap();
}

/// Index corpus: raw `index.v1.json` bytes WRITTEN BY THE ORACLE through every code path that
/// writes the index (create, store populated/reused/repaired, materialize pins, unpin, evict),
/// over keys with unicode, quotes, control characters, 256-character fields and 150 entries.
/// Each is opened, every entry verifies, and rewriting the index with no change reproduces
/// the oracle's bytes exactly.
#[test]
fn every_index_the_oracle_writes_is_accepted_and_rewritten_byte_for_byte() {
    let corpus: Value = serde_json::from_str(include_str!("fixtures/index-corpus.json")).unwrap();
    let mut entries_seen = 0;
    for case in corpus["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let raw = case["index_raw"].as_str().unwrap();
        let doc: Value = serde_json::from_str(raw).unwrap();
        let ws = test_temp::directory("index-corpus");
        let root = ws.join("cache");
        fs::create_dir(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        for dir in ["objects", "quarantine"] {
            fs::create_dir(root.join(dir)).unwrap();
            fs::set_permissions(root.join(dir), fs::Permissions::from_mode(0o700)).unwrap();
        }
        fs::write(root.join("index.v1.json"), raw).unwrap();
        for (id, spec) in case["objects"].as_object().unwrap() {
            let path = root.join("objects").join(&id[7..]);
            fs::write(
                &path,
                vec![spec["byte"].as_u64().unwrap() as u8; spec["size"].as_u64().unwrap() as usize],
            )
            .unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o400)).unwrap();
        }
        let cache = Cache::open(
            &root,
            Bytes::new(case["max_bytes"].as_u64().unwrap()),
            Bytes::new(8 * 1024 * 1024),
            Bytes::new(1024 * 1024),
        )
        .unwrap_or_else(|e| panic!("{name}: oracle-written index refused: {e}"));
        let entries = doc["entries"].as_object().unwrap();
        let status = cache.status().unwrap();
        assert_eq!(status["entry_count"], entries.len(), "{name}");
        let pinned = entries
            .values()
            .filter(|e| !e["pins"].as_array().unwrap().is_empty())
            .count();
        assert_eq!(status["pinned_object_count"], pinned, "{name}");
        for (id, entry) in entries {
            let key: ArtifactKey = serde_json::from_value(entry["key"].clone()).unwrap();
            // Independent expectation: the oracle's own id for this key.
            assert_eq!(&key.object_id().unwrap(), id, "{name}");
            if case["objects"].get(id).is_some() {
                cache
                    .verify(&key)
                    .unwrap_or_else(|e| panic!("{name}: {id}: {e}"));
            }
            entries_seen += 1;
        }
        cache
            .unpin_assignment("assignment-that-never-pinned")
            .unwrap();
        assert_eq!(
            fs::read_to_string(root.join("index.v1.json")).unwrap(),
            raw,
            "{name}: rewrite differs from the oracle's bytes"
        );
        drop(cache);
        fs::remove_dir_all(ws).unwrap();
    }
    assert_eq!(
        entries_seen,
        serde_json::from_str::<Value>(include_str!("fixtures/cache-compat-provenance.json"))
            .unwrap()["counts"]["corpus_entries"]
            .as_u64()
            .unwrap()
    );
}

/// v0.1.4 never creates a hard link (proof in the round-8 report), so a hard-linked object is a
/// user-made state. It is re-owned: the verified bytes become a fresh private 0400 inode in
/// place of the linked name, the other name keeps its bytes and its mode, nothing is
/// quarantined, and the cache converges (the next `store` reuses it).
#[test]
fn hardlinked_object_is_reowned_for_staging_and_the_other_name_is_untouched() {
    let ws = test_temp::directory("hardlink-reown");
    let spec = json!([9, 64]);
    let key = key_for(&spec, None);
    let source = ws.join("source");
    fs::write(&source, content(&spec)).unwrap();
    let cache = Cache::open(
        &ws.join("cache"),
        Bytes::new(1000),
        Bytes::new(8 * 1024 * 1024),
        Bytes::new(1024 * 1024),
    )
    .unwrap();
    cache.store(&key, &source).unwrap();
    let object = ws
        .join("cache/objects")
        .join(&key.object_id().unwrap()[7..]);
    let other = ws.join("other-name");
    fs::hard_link(&object, &other).unwrap();
    assert_eq!(fs::metadata(&object).unwrap().nlink(), 2);
    // Read-only verification serves it (v0.1.4 offline startup uses it) and mutates nothing;
    // staging then converges the object to a private inode.
    let index_before = fs::read(ws.join("cache/index.v1.json")).unwrap();
    assert!(cache.verify(&key).is_ok());
    assert_eq!(fs::metadata(&object).unwrap().nlink(), 2);
    assert_eq!(
        fs::read(ws.join("cache/index.v1.json")).unwrap(),
        index_before
    );
    let required = BTreeMap::from([("w.bin".to_owned(), key.clone())]);
    cache
        .materialize_assignment("a", &required, &ws.join("stage/a"))
        .unwrap();
    let meta = fs::metadata(&object).unwrap();
    assert_eq!((meta.nlink(), meta.mode() & 0o7777), (1, 0o400));
    assert_eq!(fs::metadata(&other).unwrap().nlink(), 1);
    assert_eq!(fs::read(&other).unwrap(), content(&spec));
    assert_eq!(fs::read(&object).unwrap(), content(&spec));
    assert_eq!(
        fs::read_dir(ws.join("cache/quarantine")).unwrap().count(),
        0
    );
    assert!(cache.verify(&key).is_ok());
    drop(cache);
    fs::remove_dir_all(ws).unwrap();
}

/// Proof for ruling 4: the oracle can write one object under two staged paths, but the manifest
/// it returns is rejected by its own `validate_materialization_report` (duplicate object ids,
/// and a negative `unassigned_object_count` when the cache holds fewer entries than paths).
/// The refusal here is therefore the user-visible v0.1.4 outcome of the full contract.
#[test]
fn one_object_under_two_staged_paths_is_refused_like_the_v014_report_contract() {
    let f = compat_fixture();
    let proof = &f["proofs"]["duplicate_object_report"];
    assert_eq!(proof["validator"]["ok"], false);
    assert_eq!(
        proof["validator"]["message"],
        "assignment materialization report is invalid"
    );
    assert_eq!(proof["manifest"]["unassigned_object_count"], -1);
    let ws = test_temp::directory("dup-object");
    let spec = json!([5, 40]);
    let key = key_for(&spec, None);
    fs::write(ws.join("source"), content(&spec)).unwrap();
    let cache = Cache::open(
        &ws.join("cache"),
        Bytes::new(1000),
        Bytes::new(8 * 1024 * 1024),
        Bytes::new(1024 * 1024),
    )
    .unwrap();
    cache.store(&key, &ws.join("source")).unwrap();
    let required = BTreeMap::from([
        ("one.bin".to_owned(), key.clone()),
        ("sub/two.bin".to_owned(), key.clone()),
    ]);
    let error = cache
        .materialize_assignment("a", &required, &ws.join("stage/a"))
        .unwrap_err();
    assert_eq!(error.code, "artifact_assignment_invalid");
    assert!(!ws.join("stage/a").exists());
    assert_eq!(cache.status().unwrap()["pinned_object_count"], 0);
    drop(cache);
    fs::remove_dir_all(ws).unwrap();
}

/// The root is canonicalised once at `open` (integrator ruling 2): a symlinked parent works, and
/// re-pointing that symlink afterwards cannot move the cache under the open handle.
#[test]
fn the_canonical_root_is_pinned_at_open_when_a_parent_symlink_is_repointed() {
    let ws = test_temp::directory("repointed-parent");
    for dir in ["real-1", "real-2"] {
        fs::create_dir(ws.join(dir)).unwrap();
    }
    symlink(ws.join("real-1"), ws.join("link")).unwrap();
    let spec = json!([3, 32]);
    let key = key_for(&spec, None);
    fs::write(ws.join("source"), content(&spec)).unwrap();
    let cache = Cache::open(
        &ws.join("link/cache"),
        Bytes::new(1000),
        Bytes::new(8 * 1024 * 1024),
        Bytes::new(1024 * 1024),
    )
    .unwrap();
    fs::remove_file(ws.join("link")).unwrap();
    symlink(ws.join("real-2"), ws.join("link")).unwrap();
    cache.store(&key, &ws.join("source")).unwrap();
    assert!(cache.verify(&key).is_ok());
    assert_eq!(
        fs::read_dir(ws.join("real-1/cache/objects"))
            .unwrap()
            .count(),
        1
    );
    assert!(!ws.join("real-2/cache").exists());
    drop(cache);
    fs::remove_dir_all(ws).unwrap();
}

/// v0.1.4 locates bytes from the key it is asked for and never reads the entry's own `key`,
/// so an edited `key` field cannot make a read refuse (and cannot redirect anything: the bytes
/// are hashed against the requested key).
#[test]
fn an_edited_entry_key_field_does_not_stop_the_requested_key_from_serving() {
    let ws = test_temp::directory("edited-entry-key");
    let spec = json!([4, 48]);
    let key = key_for(&spec, None);
    fs::write(ws.join("source"), content(&spec)).unwrap();
    let open = || {
        Cache::open(
            &ws.join("cache"),
            Bytes::new(1000),
            Bytes::new(8 * 1024 * 1024),
            Bytes::new(1024 * 1024),
        )
        .unwrap()
    };
    let cache = open();
    cache.store(&key, &ws.join("source")).unwrap();
    drop(cache);
    let index = ws.join("cache/index.v1.json");
    let mut doc: Value = serde_json::from_slice(&fs::read(&index).unwrap()).unwrap();
    let id = key.object_id().unwrap();
    doc["entries"][&id]["key"]["model_revision"] = json!("someone-else");
    fs::write(&index, serde_json::to_vec(&doc).unwrap()).unwrap();
    let cache = open();
    assert!(cache.verify(&key).is_ok());
    let required = BTreeMap::from([("w.bin".to_owned(), key.clone())]);
    assert!(cache.resolve_offline(&required).is_ok());
    drop(cache);
    fs::remove_dir_all(ws).unwrap();
}

/// A symlink swapped in for `objects/` or `quarantine/` AFTER open is refused on the next
/// operation (the integrator's ruling refuses symlinks inside the root; ancestors above it are
/// pinned by the canonical root).
#[test]
fn a_directory_inside_the_root_swapped_for_a_symlink_after_open_is_refused() {
    for dir in ["objects", "quarantine"] {
        let ws = test_temp::directory("swapped-directory");
        let spec = json!([6, 20]);
        let key = key_for(&spec, None);
        fs::write(ws.join("source"), content(&spec)).unwrap();
        let cache = Cache::open(
            &ws.join("cache"),
            Bytes::new(1000),
            Bytes::new(8 * 1024 * 1024),
            Bytes::new(1024 * 1024),
        )
        .unwrap();
        fs::create_dir(ws.join("elsewhere")).unwrap();
        fs::remove_dir(ws.join("cache").join(dir)).unwrap();
        symlink(ws.join("elsewhere"), ws.join("cache").join(dir)).unwrap();
        let required = BTreeMap::from([("w.bin".to_owned(), key.clone())]);
        // Every operation takes the same gate; none follows the link or writes through it.
        assert_eq!(
            cache.store(&key, &ws.join("source")).unwrap_err().code,
            "artifact_unsafe",
            "{dir}"
        );
        assert_eq!(cache.verify(&key).unwrap_err().code, "artifact_unsafe");
        assert_eq!(cache.evict().unwrap_err().code, "artifact_unsafe");
        assert_eq!(cache.status().unwrap_err().code, "artifact_unsafe");
        assert_eq!(
            cache
                .materialize_assignment("a", &required, &ws.join("stage/a"))
                .unwrap_err()
                .code,
            "artifact_unsafe"
        );
        assert!(!ws.join("stage/a").exists());
        assert_eq!(fs::read_dir(ws.join("elsewhere")).unwrap().count(), 0);
        drop(cache);
        fs::remove_dir_all(ws).unwrap();
    }
}

/// A symlinked OBJECT FILE: read-only paths refuse it and never follow it; `store` does what
/// v0.1.4 does with it (replay `symlink-to-valid-other-object`): quarantine the link and
/// re-copy, the link target is not modified.
#[test]
fn a_symlinked_object_file_is_never_followed_and_store_replaces_it_as_v014_does() {
    let ws = test_temp::directory("symlinked-object");
    let spec = json!([8, 24]);
    let key = key_for(&spec, None);
    fs::write(ws.join("source"), content(&spec)).unwrap();
    let cache = Cache::open(
        &ws.join("cache"),
        Bytes::new(1000),
        Bytes::new(8 * 1024 * 1024),
        Bytes::new(1024 * 1024),
    )
    .unwrap();
    cache.store(&key, &ws.join("source")).unwrap();
    let object = ws
        .join("cache/objects")
        .join(&key.object_id().unwrap()[7..]);
    fs::remove_file(&object).unwrap();
    fs::write(ws.join("target"), content(&spec)).unwrap();
    symlink(ws.join("target"), &object).unwrap();
    let required = BTreeMap::from([("w.bin".to_owned(), key.clone())]);
    assert_eq!(cache.verify(&key).unwrap_err().code, "artifact_unsafe");
    assert_eq!(
        cache
            .materialize_assignment("a", &required, &ws.join("stage/a"))
            .unwrap_err()
            .code,
        "artifact_unsafe"
    );
    assert_eq!(
        cache.store(&key, &ws.join("source")).unwrap(),
        artifacts::StoreOutcome::Repaired
    );
    assert!(
        !fs::symlink_metadata(&object)
            .unwrap()
            .file_type()
            .is_symlink()
    );
    assert_eq!(fs::read(ws.join("target")).unwrap(), content(&spec));
    assert!(cache.verify(&key).is_ok());
    drop(cache);
    fs::remove_dir_all(ws).unwrap();
}
