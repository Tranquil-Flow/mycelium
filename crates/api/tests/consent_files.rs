// SPDX-License-Identifier: AGPL-3.0-only
//! Consent files against the EXECUTED v0.1.4 oracle (`fixtures/oracle_consent.json`, made by
//! `L10-evidence/r7-probe-consent.py`, which imports the read-only package at f4e366e9 and calls
//! `app.consent_valid`, `app.read_resource_policy`, and the `standing_consent` grant / bind /
//! withdraw / load functions on Python 3.14.7): validity, byte-exact files, modes, refusal codes
//! and their public status classes.
use api::preferences::{resource_policy, standing_consent as sc};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
};

fn oracle() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_consent.json")).unwrap()
}
fn root(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("l10-consent-{tag}-{}", std::process::id()));
    if dir.exists() {
        fs::remove_dir_all(&dir).unwrap();
    }
    fs::create_dir_all(&dir).unwrap();
    dir
}
fn put(path: &Path, raw: &[u8], mode: u32) {
    fs::write(path, raw).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}
fn outcome<T: Into<Value>>(r: Result<T, api::schema::PublicError>) -> Value {
    match r {
        Ok(v) => json!({"ok": v.into()}),
        Err(e) => json!({"err": {"status": e.status, "code": e.code}}),
    }
}
fn mode(path: &Path) -> String {
    format!("0o{:o}", fs::metadata(path).unwrap().mode() & 0o7777)
}
fn leftovers(dir: &Path, keep: &str) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .filter(|n| n != keep)
        .collect();
    names.sort();
    names
}

#[test]
fn consent_valid_matches_app_consent_valid() {
    let oracle = oracle();
    assert_eq!(
        oracle["storage_ceiling"],
        api::model_catalog::storage_consent_ceiling()
    );
    let cases = oracle["consent_valid"].as_array().unwrap();
    assert_eq!(cases.len(), 23);
    for case in cases {
        assert_eq!(
            resource_policy::consent_valid(&case["consent"]),
            case["valid"].as_bool().unwrap(),
            "{}",
            case["label"]
        );
    }
}

#[test]
fn read_policy_matches_read_resource_policy_over_file_states() {
    let oracle = oracle();
    let cases = oracle["read_policy"].as_array().unwrap();
    assert_eq!(cases.len(), 20);
    for case in cases {
        let dir = root("read");
        let file = dir.join(resource_policy::FILE);
        let label = case["label"].as_str().unwrap();
        if let Some(raw) = case["file"].as_str() {
            // The probe stored the bytes as latin-1 text.
            let bytes: Vec<u8> = raw.chars().map(|c| c as u32 as u8).collect();
            let raw_mode =
                u32::from_str_radix(case["mode"].as_str().unwrap().trim_start_matches("0o"), 8)
                    .unwrap();
            put(&file, &bytes, raw_mode);
            if case["link"] == true {
                fs::hard_link(&file, dir.join("other")).unwrap();
            }
            if case["extra"] == "symlink" {
                fs::remove_file(&file).unwrap();
                put(&dir.join("real"), &bytes, 0o600);
                symlink(dir.join("real"), &file).unwrap();
            }
        }
        let got = resource_policy::read(&dir).map(|v| v.map_or(Value::Null, |m| m));
        let want = if case.get("ok").is_some() {
            json!({"ok": case["ok"]})
        } else {
            json!({"err": case["err"]})
        };
        assert_eq!(outcome(got), want, "{label}");
        fs::remove_dir_all(dir).unwrap();
    }
}

#[test]
fn write_policy_is_python_json_dumps_sorted_with_private_mode() {
    let oracle = oracle();
    let good = oracle["consent_valid"][0]["consent"].clone();
    let dir = root("write");
    resource_policy::write(&dir, &good).unwrap();
    let file = dir.join(resource_policy::FILE);
    // json.dumps(consent, sort_keys=True): ", " and ": " separators.
    let want = oracle["create_cases"][0]["file"].as_str().unwrap();
    assert_eq!(fs::read_to_string(&file).unwrap(), want);
    assert_eq!(mode(&file), oracle["create_cases"][0]["mode"]);
    assert_eq!(fs::metadata(&file).unwrap().nlink(), 1);
    assert!(leftovers(&dir, resource_policy::FILE).is_empty());
    // The written file is accepted back by the reader.
    assert_eq!(resource_policy::read(&dir).unwrap().unwrap(), good);
    // Replacing keeps one file and no temporary.
    let mut other = good.clone();
    other["power"] = json!("battery_allowed");
    resource_policy::write(&dir, &other).unwrap();
    assert_eq!(resource_policy::read(&dir).unwrap().unwrap(), other);
    assert!(leftovers(&dir, resource_policy::FILE).is_empty());
    // A missing directory is the builtin OSError class.
    let e = resource_policy::write(Path::new("/nonexistent-l10-r7/x"), &good).unwrap_err();
    assert_eq!((e.status, e.code), (409, "action_not_completed"));
    fs::remove_dir_all(dir).unwrap();
}

fn state_error(r: Result<impl Into<Value>, sc::StandingError>) -> Value {
    match r {
        Ok(v) => json!({"ok": v.into()}),
        Err(e) => json!({"err": e.0}),
    }
}
#[test]
fn disclosure_text_and_digest_are_the_shipped_ones() {
    use sha2::{Digest, Sha256};
    let oracle = oracle();
    assert_eq!(
        sc::DISCLOSURE_TEXT,
        oracle["disclosure"]["text"].as_str().unwrap()
    );
    assert_eq!(
        sc::DISCLOSURE_DIGEST,
        oracle["disclosure"]["digest"].as_str().unwrap()
    );
    let digest: String = Sha256::digest(sc::DISCLOSURE_TEXT.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    assert_eq!(sc::DISCLOSURE_DIGEST, format!("sha256:{digest}"));
}

/// Replays one oracle step list against the Rust functions; the fixture records the op names
/// and arguments by convention of the probe (`BODY`, `BIND`, `POLICY`, fixed clocks).
fn replay(label: &str, op: &str, root: &Path, oracle: &Value) -> Value {
    let caps = json!({"storage_bytes":1_000_000_000,"max_memory_bytes":4u64*1024*1024*1024,"max_wall_seconds":900,"max_commands":50});
    let body = json!({"acknowledged":true,"disclosure_digest":sc::DISCLOSURE_DIGEST,"caps":caps});
    let policy = oracle["consent_valid"][0]["consent"].clone();
    let bind_doc = json!({"swarm_id":"sw-aaaaaaaaaaaaaaaaaaaa","node_id":"node-1","seed_key_digest":format!("sha256:{}","ab".repeat(32))});
    let with_caps = |k: &str, v: Value| {
        let mut c = caps.clone();
        c[k] = v;
        json!({"acknowledged":true,"disclosure_digest":sc::DISCLOSURE_DIGEST,"caps":c})
    };
    let grant = |b: &Value, now: f64, mem: Option<u64>| sc::grant(root, b, &policy, now, mem);
    match (label, op) {
        ("whole_second_float", "grant") => {
            state_error(grant(&body, 1761600000.0, None).map(Value::Object))
        }
        ("whole_second_float", "withdraw") => state_error(
            sc::withdraw(root, 1761600005.0).map(|m| m.map_or(Value::Null, Value::Object)),
        ),
        (_, "load") => state_error(sc::load(root).map(|m| m.map_or(Value::Null, Value::Object))),
        (_, "withdraw_missing" | "withdraw") => state_error(
            sc::withdraw(root, 1761600200.0).map(|m| m.map_or(Value::Null, Value::Object)),
        ),
        (_, "withdraw_again") => state_error(
            sc::withdraw(root, 1761700000.0).map(|m| m.map_or(Value::Null, Value::Object)),
        ),
        (_, "bind_missing" | "bind" | "bind_withdrawn" | "bind_ok") => {
            state_error(sc::bind(root, &bind_doc, 1761600100.25).map(Value::Object))
        }
        (_, "bind_again_same") => {
            state_error(sc::bind(root, &bind_doc, 1761600999.0).map(Value::Object))
        }
        (_, "bind_other") => {
            let mut other = bind_doc.clone();
            other["node_id"] = json!("node-2");
            state_error(sc::bind(root, &other, 1761600100.25).map(Value::Object))
        }
        (_, "bind_missing_key") => state_error(
            sc::bind(root, &json!({"swarm_id":"a","node_id":"b"}), 1.0).map(Value::Object),
        ),
        (_, "bind_empty") => {
            let mut b = bind_doc.clone();
            b["node_id"] = json!("");
            state_error(sc::bind(root, &b, 1.0).map(Value::Object))
        }
        (_, "bind_extra") => {
            let mut b = bind_doc.clone();
            b["x"] = json!("y");
            state_error(sc::bind(root, &b, 1.0).map(Value::Object))
        }
        (_, "bind_int") => {
            let mut b = bind_doc.clone();
            b["node_id"] = json!(5);
            state_error(sc::bind(root, &b, 1.0).map(Value::Object))
        }
        ("lifecycle", "grant") => state_error(grant(&body, 1761600000.5, None).map(Value::Object)),
        ("lifecycle", "grant_same_caps_later_time") => {
            state_error(grant(&body, 1761609999.0, None).map(Value::Object))
        }
        ("lifecycle", "grant_changed_caps") => state_error(
            grant(&with_caps("max_commands", json!(51)), 1761600000.5, None).map(Value::Object),
        ),
        ("lifecycle", "regrant_after_withdraw") => {
            state_error(grant(&body, 1761800000.75, None).map(Value::Object))
        }
        ("bind_refusals", "grant") => state_error(grant(&body, 1.5, None).map(Value::Object)),
        ("grant_refusals", op) => {
            let mut b = body.clone();
            let mut mem = None;
            match op {
                "no_ack" => b["acknowledged"] = json!(false),
                "ack_int" => b["acknowledged"] = json!(1),
                "bad_digest" => b["disclosure_digest"] = json!("sha256:00"),
                "extra_key" => b["x"] = json!(1),
                "not_mapping" => b = json!("x"),
                "caps_missing_key" => {
                    b["caps"].as_object_mut().unwrap().remove("max_commands");
                }
                "caps_storage_low" => b["caps"]["storage_bytes"] = json!(99_999_999),
                "caps_storage_above_policy" => b["caps"]["storage_bytes"] = json!(1_000_000_001u64),
                "caps_memory_low" => {
                    b["caps"]["max_memory_bytes"] = json!(256u64 * 1024 * 1024 - 1)
                }
                "caps_memory_high" => {
                    b["caps"]["max_memory_bytes"] = json!(512u64 * 1024 * 1024 * 1024 + 1)
                }
                "caps_wall_low" => b["caps"]["max_wall_seconds"] = json!(59),
                "caps_wall_high" => b["caps"]["max_wall_seconds"] = json!(3601),
                "caps_commands_zero" => b["caps"]["max_commands"] = json!(0),
                "caps_bool" => b["caps"]["max_commands"] = json!(true),
                "memory_exceeds_device" => mem = Some(4u64 * 1024 * 1024 * 1024),
                "memory_within_device" => mem = Some(8u64 * 1024 * 1024 * 1024),
                other => panic!("unknown step {other}"),
            }
            state_error(grant(&b, 1761600000.5, mem).map(Value::Object))
        }
        (l, o) => panic!("unmapped oracle step {l}/{o}"),
    }
}

#[test]
fn standing_consent_lifecycle_files_and_refusals_match_the_oracle() {
    let oracle = oracle();
    let mut steps = 0;
    for record in oracle["standing_steps"].as_array().unwrap() {
        let label = record["label"].as_str().unwrap();
        if label.starts_with("time_") {
            continue;
        }
        let dir = root("standing");
        let file = dir.join(sc::FILE);
        for step in record["steps"].as_array().unwrap() {
            let op = step["op"].as_str().unwrap();
            let got = replay(label, op, &dir, &oracle);
            let want = match step.get("err") {
                Some(e) => json!({"err": e["code"]}),
                None => json!({"ok": step["ok"]}),
            };
            assert_eq!(got, want, "{label}/{op}");
            match step["file"].as_str() {
                Some(raw) => {
                    assert_eq!(fs::read_to_string(&file).unwrap(), raw, "{label}/{op}");
                    assert_eq!(mode(&file), step["mode"], "{label}/{op}");
                }
                None => assert!(!file.exists(), "{label}/{op}"),
            }
            steps += 1;
        }
        assert_eq!(
            leftovers(&dir, sc::FILE),
            record["leftovers"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_string())
                .collect::<Vec<_>>()
        );
        fs::remove_dir_all(dir).unwrap();
    }
    assert_eq!(steps, 40);
}

#[test]
fn grant_time_must_be_finite() {
    let oracle = oracle();
    let policy = oracle["consent_valid"][0]["consent"].clone();
    let caps = json!({"storage_bytes":1_000_000_000,"max_memory_bytes":4u64*1024*1024*1024,"max_wall_seconds":900,"max_commands":50});
    let body = json!({"acknowledged":true,"disclosure_digest":sc::DISCLOSURE_DIGEST,"caps":caps});
    // The Python bool/str/None clocks are unrepresentable here; NaN and infinity are the rest.
    for now in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        let dir = root("time");
        assert_eq!(
            sc::grant(&dir, &body, &policy, now, None).unwrap_err(),
            sc::StandingError("standing_consent_time_invalid")
        );
        assert!(!dir.join(sc::FILE).exists());
        fs::remove_dir_all(dir).unwrap();
    }
    let ok = oracle["standing_steps"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["label"] == "time_int_ok")
        .unwrap();
    let dir = root("time-ok");
    sc::grant(&dir, &body, &policy, 7.0, None).unwrap();
    assert_eq!(
        fs::read_to_string(dir.join(sc::FILE)).unwrap(),
        ok["steps"][0]["file"].as_str().unwrap()
    );
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn corrupt_standing_files_fail_every_operation_with_the_oracle_code() {
    let oracle = oracle();
    let cases = oracle["standing_corrupt"].as_array().unwrap();
    assert_eq!(cases.len(), 11);
    let policy = oracle["consent_valid"][0]["consent"].clone();
    let caps = json!({"storage_bytes":1_000_000_000,"max_memory_bytes":4u64*1024*1024*1024,"max_wall_seconds":900,"max_commands":50});
    let body = json!({"acknowledged":true,"disclosure_digest":sc::DISCLOSURE_DIGEST,"caps":caps});
    let bind_doc = json!({"swarm_id":"sw-aaaaaaaaaaaaaaaaaaaa","node_id":"node-1","seed_key_digest":format!("sha256:{}","ab".repeat(32))});
    for case in cases {
        let dir = root("corrupt-standing");
        let file = dir.join(sc::FILE);
        // Rewrite the exact file the probe produced, including its mode.
        let m = if case["label"] == "mode_0644" {
            0o644
        } else {
            0o600
        };
        put(&file, case["file"].as_str().unwrap().as_bytes(), m);
        let label = case["label"].as_str().unwrap();
        let results = [
            (
                "load",
                state_error(sc::load(&dir).map(|m| m.map_or(Value::Null, Value::Object))),
            ),
            (
                "withdraw",
                state_error(
                    sc::withdraw(&dir, 1761600200.0).map(|m| m.map_or(Value::Null, Value::Object)),
                ),
            ),
            (
                "grant",
                state_error(sc::grant(&dir, &body, &policy, 1761600000.5, None).map(Value::Object)),
            ),
            (
                "bind",
                state_error(sc::bind(&dir, &bind_doc, 1761600100.25).map(Value::Object)),
            ),
        ];
        for (op, got) in results {
            let want = &case[op];
            if let Some(err) = want.get("err") {
                // Both StandingError and the authority error surface their `code`.
                assert_eq!(
                    got,
                    json!({"err": want["code"].as_str().unwrap_or(err["code"].as_str().unwrap())}),
                    "{label}/{op}"
                );
            } else {
                assert!(got.get("ok").is_some(), "{label}/{op}: {got}");
            }
            // Re-put the corrupt bytes for the next operation (valid files get rewritten by ops).
            put(&file, case["file"].as_str().unwrap().as_bytes(), m);
        }
        fs::remove_dir_all(dir).unwrap();
    }
}
