// SPDX-License-Identifier: AGPL-3.0-only
//! The adaptive host policy file against the EXECUTED oracle (FX-L10-ADAPTIVE-POLICY:
//! `L10-evidence/r12-probe-adaptive-policy.py` -> `fixtures/oracle_adaptive_policy.json`, v0.1.4 f4e366e9):
//! `read_adaptive_policy` (93 files: encodings, modes, links, size, every validation branch),
//! `write_adaptive_policy` (17 cases: authority, normalisation, bytes, replacement), the
//! `apply_first_consent_choice` file effects (15) and the off/error branches of `adaptive_status` (7).
use api::adaptive::{self, Failure};
use serde_json::{Map, Value, json};
use std::{fs, os::unix::fs::PermissionsExt, path::Path};

fn doc() -> Value {
    let doc: Value =
        serde_json::from_str(include_str!("fixtures/oracle_adaptive_policy.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    doc
}
fn unhex(hex: &str) -> Vec<u8> {
    (0..hex.len() / 2)
        .map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).unwrap())
        .collect()
}
fn fresh(kind: &str, label: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "l10r12-adaptive-{}-{kind}-{label}",
        std::process::id()
    ));
    fs::remove_dir_all(&dir).unwrap_or(());
    fs::create_dir_all(&dir).unwrap();
    dir
}
fn put(path: &Path, bytes: &[u8], mode: u32) {
    fs::write(path, bytes).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}
/// `lay()` of the probe.
fn lay(dir: &Path, spec: &Value) {
    let path = dir.join(adaptive::FILE);
    match spec["kind"].as_str().unwrap() {
        "file" => put(
            &path,
            &unhex(spec["hex"].as_str().unwrap()),
            u32::from_str_radix(spec["mode"].as_str().unwrap_or("0600"), 8).unwrap(),
        ),
        "symlink" => {
            put(&dir.join("elsewhere.json"), &canonical_default(), 0o600);
            std::os::unix::fs::symlink("elsewhere.json", &path).unwrap();
        }
        "hardlink" => {
            put(&dir.join("other.json"), &canonical_default(), 0o600);
            fs::hard_link(dir.join("other.json"), &path).unwrap();
        }
        "dir" => fs::create_dir(&path).unwrap(),
        "none" => {}
        other => panic!("{other}"),
    }
}
/// `json.dumps(DEFAULT_POLICY, sort_keys=True)`.
fn canonical_default() -> Vec<u8> {
    br#"{"cpu_saturation_fraction": null, "enabled": false, "game_mode": false, "reserve_bytes": 0, "schema": 1, "timings": {}}"#.to_vec()
}
fn listing(dir: &Path) -> Value {
    let mut out = Map::new();
    let mut names: Vec<_> = fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    names.sort();
    for path in names {
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        let meta = fs::symlink_metadata(&path).unwrap();
        let entry = if meta.file_type().is_symlink() {
            json!({"symlink": fs::read_link(&path).unwrap().to_string_lossy()})
        } else if meta.is_file() {
            let mode = meta.permissions().mode() & 0o777;
            let readable = mode & 0o400 != 0;
            if !readable {
                fs::set_permissions(&path, fs::Permissions::from_mode(mode | 0o400)).unwrap();
            }
            let raw = fs::read(&path).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
            json!({"hex": raw.iter().map(|b| format!("{b:02x}")).collect::<String>(),
                   "mode": format!("{mode:04o}")})
        } else {
            json!({"dir": true})
        };
        out.insert(name, entry);
    }
    Value::Object(out)
}
fn describe(failure: &Failure) -> (String, String, Option<String>) {
    match failure {
        Failure::Adaptive(code) => (
            "AdaptiveHostError".into(),
            "mycelium_a13_macos.adaptive_host".into(),
            Some((*code).into()),
        ),
        Failure::Os(error) => {
            let (class, module) = error.raised.identity();
            (class.into(), module.into(), None)
        }
    }
}
fn same_failure(got: &Failure, want: &Value) -> bool {
    let (class, module, code) = describe(got);
    class == want["type"].as_str().unwrap()
        && module == want["module"].as_str().unwrap()
        && code.as_deref() == want["code"].as_str()
}

#[test]
fn the_default_policy_is_the_oracles() {
    assert_eq!(
        Value::Object(adaptive::default_policy()),
        doc()["default_policy"]
    );
}

#[test]
fn read_adaptive_policy_equals_the_executed_oracle() {
    let doc = doc();
    let cases = doc["reads"].as_array().unwrap();
    assert!(cases.len() >= 93, "{} cases", cases.len());
    let mut failures = Vec::new();
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let dir = fresh("r", label);
        lay(&dir, &case["spec"]);
        let got = adaptive::read(&dir);
        let verdict = match (&got, case.get("ok"), case.get("raised")) {
            (Ok(policy), Some(want), None) => (&Value::Object(policy.clone()) == want)
                .then_some(())
                .ok_or_else(|| format!("policy {policy:?} != {want}")),
            (Err(failure), None, Some(want)) => same_failure(failure, want)
                .then_some(())
                .ok_or_else(|| format!("{:?} != {want}", describe(failure))),
            (got, ok, raised) => Err(format!("{got:?} vs {ok:?}/{raised:?}")),
        };
        if let Err(problem) = verdict {
            failures.push(format!("read {label}: {problem}"));
        }
        if listing(&dir) != case["files"] {
            failures.push(format!("read {label}: files differ"));
        }
        fs::remove_dir_all(&dir).unwrap_or(());
    }
    assert!(
        failures.is_empty(),
        "{} differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

#[test]
fn write_adaptive_policy_equals_the_executed_oracle() {
    let doc = doc();
    let cases = doc["writes"].as_array().unwrap();
    assert!(cases.len() >= 17);
    let mut failures = Vec::new();
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let dir = fresh("w", label);
        let path = dir.join(adaptive::FILE);
        match case["pre"].as_str() {
            Some("symlink") => std::os::unix::fs::symlink("nowhere", &path).unwrap(),
            Some("file") => put(&path, br#"{"enabled": true}"#, 0o600),
            Some("dir") => fs::create_dir(&path).unwrap(),
            _ => {}
        }
        // Only `True` is authority: the probe also offered None, 1 and "yes".
        let owner = case["owner_authorized"] == Value::Bool(true);
        let got = adaptive::write(&dir, &case["policy"], owner);
        let verdict = match (&got, case.get("ok"), case.get("raised")) {
            (Ok(()), Some(_), None) => Ok(()),
            (Err(failure), None, Some(want)) => same_failure(failure, want)
                .then_some(())
                .ok_or_else(|| format!("{:?} != {want}", describe(failure))),
            (got, ok, raised) => Err(format!("{got:?} vs {ok:?}/{raised:?}")),
        };
        if let Err(problem) = verdict {
            failures.push(format!("write {label}: {problem}"));
        }
        if listing(&dir) != case["files"] {
            failures.push(format!(
                "write {label}: files {} != {}",
                listing(&dir),
                case["files"]
            ));
        }
        fs::remove_dir_all(&dir).unwrap_or(());
    }
    assert!(
        failures.is_empty(),
        "{} differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

#[test]
fn the_first_consent_choice_writes_only_when_no_policy_exists() {
    let doc = doc();
    let cases = doc["first_consent"].as_array().unwrap();
    assert!(cases.len() >= 15);
    let mut failures = Vec::new();
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let dir = fresh("c", label);
        let path = dir.join(adaptive::FILE);
        match case["pre"].as_str() {
            Some("valid") => put(&path, br#"{"enabled": true}"#, 0o600),
            Some("invalid") => put(&path, b"{nope", 0o600),
            Some("dangling_symlink") => std::os::unix::fs::symlink("nowhere", &path).unwrap(),
            Some("dir") => fs::create_dir(&path).unwrap(),
            _ => {}
        }
        // `pre` files above hold whatever bytes the probe wrote; the effect under test is "untouched".
        let before = listing(&dir);
        let result = adaptive::apply_first_consent_choice(&dir, case["choice"].as_bool());
        if result.is_err() != case.get("raised").is_some() {
            failures.push(format!("consent {label}: {result:?}"));
        }
        let after = listing(&dir);
        if case["pre"].is_null() {
            if after != case["files"] {
                failures.push(format!(
                    "consent {label}: files {after} != {}",
                    case["files"]
                ));
            }
        } else if after != before {
            failures.push(format!("consent {label}: an existing policy was changed"));
        }
        fs::remove_dir_all(&dir).unwrap_or(());
    }
    assert!(
        failures.is_empty(),
        "{} differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

#[test]
fn adaptive_status_off_and_error_branches_equal_the_executed_oracle() {
    let doc = doc();
    let cases = doc["status"].as_array().unwrap();
    assert!(cases.len() >= 7);
    let mut failures = Vec::new();
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let dir = fresh("s", label);
        lay(&dir, &case["spec"]);
        let got = adaptive::static_status(&dir);
        match (got, case.get("ok")) {
            (Some(status), Some(want)) if &status.to_value() == want => {}
            (got, want) => failures.push(format!("status {label}: {got:?} != {want:?}")),
        }
        fs::remove_dir_all(&dir).unwrap_or(());
    }
    assert!(
        failures.is_empty(),
        "{} differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
