// SPDX-License-Identifier: AGPL-3.0-only
//! Run-model picker document and choice action against the EXECUTED v0.1.4 oracle: every
//! expected value in `fixtures/oracle_run_model.json` was produced by importing the read-only
//! package at f4e366e9 and calling `run_model.document`, `model_resources.consent_document`
//! and `DemoApp._choose_run_model` on Python 3.14.7 (`L10-evidence/r7-probe-run-model.py`).
use api::{
    model_catalog,
    preferences::run_model::{self, Facts},
};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
};

fn oracle() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_run_model.json")).unwrap()
}
fn root(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("l10-rmdoc-{tag}-{}", std::process::id()));
    if dir.exists() {
        fs::remove_dir_all(&dir).unwrap();
    }
    fs::create_dir_all(&dir).unwrap();
    dir
}
fn outcome(r: Result<Value, api::schema::PublicError>) -> Value {
    match r {
        Ok(v) => json!({"ok": v}),
        Err(e) => json!({"err": {"status": e.status, "code": e.code}}),
    }
}
fn expected(case: &Value) -> Value {
    match case.get("ok") {
        Some(v) => json!({"ok": v}),
        None => json!({"err": case["err"]}),
    }
}
fn memory(case: &Value) -> Option<u64> {
    case["device_memory_bytes"].as_u64()
}

#[test]
fn ceilings_and_consent_documents_equal_model_resources_py() {
    let oracle = oracle();
    assert_eq!(
        model_catalog::storage_consent_ceiling(),
        oracle["ceilings"]["storage_consent"].as_u64().unwrap()
    );
    assert_eq!(
        model_catalog::pairing_quota_ceiling(),
        oracle["ceilings"]["pairing_quota"].as_u64().unwrap()
    );
    let mut checked = 0;
    for (label, per_memory) in oracle["consent_document"].as_object().unwrap() {
        let mem = per_memory["device_memory_bytes"].as_u64();
        for (key, expect) in per_memory.as_object().unwrap() {
            if key == "device_memory_bytes" {
                continue;
            }
            let selected = if key == "none" {
                None
            } else {
                Some(expect["selected"]["model_id"].as_str().unwrap())
            };
            let commit = expect["selected"]["resolved_commit"].as_str();
            let got = model_catalog::consent_document(selected.zip(commit), mem);
            assert_eq!(&got, expect, "memory {label}, model {key}");
            checked += 1;
        }
    }
    assert_eq!(checked, 9);
}

#[test]
fn memory_ceiling_and_options_match_the_oracle() {
    for (bytes, expect) in oracle()["memory_helpers"].as_object().unwrap() {
        let bytes: u64 = bytes.parse().unwrap();
        assert_eq!(
            model_catalog::member_memory_ceiling(bytes),
            expect["ceiling"].as_u64().unwrap(),
            "{bytes}"
        );
        assert_eq!(
            json!(model_catalog::memory_cap_options(bytes)),
            expect["options"],
            "{bytes}"
        );
    }
}

#[test]
fn whole_model_member_limits_match_the_oracle() {
    for (model, expect) in oracle()["whole_model_members"].as_object().unwrap() {
        let got = model_catalog::whole_model_member(model).unwrap();
        for key in [
            "resident_bytes",
            "kv_bytes",
            "load_peak_bytes",
            "max_rss_bytes",
            "disk_need_bytes",
            "max_disk_bytes",
            "disk_footprint_bytes",
        ] {
            assert_eq!(got[key], expect[key], "{model} {key}");
        }
        assert_eq!(got["basis"], expect["basis"], "{model}");
    }
}

#[test]
fn document_matches_run_model_py_over_state_environment_and_memory() {
    let oracle = oracle();
    let cases = oracle["document_cases"].as_array().unwrap();
    assert_eq!(cases.len(), 24);
    for (i, case) in cases.iter().enumerate() {
        let dir = root(&format!("doc{i}"));
        if let Some(state) = case["state"].as_str() {
            run_model::save_choice(&dir, state).unwrap();
        }
        let got = run_model::document(&dir, case["env"].as_str(), memory(case));
        assert_eq!(
            outcome(got),
            expected(case),
            "case {i}: state {} env {} memory {}",
            case["state"],
            case["env"],
            case["memory"]
        );
        fs::remove_dir_all(dir).unwrap();
    }
}

#[test]
fn corrupt_state_surfaces_as_the_collapsed_builtin_failure() {
    for case in oracle()["document_corrupt"].as_array().unwrap() {
        let dir = root("corrupt");
        let file = dir.join(run_model::FILE);
        fs::write(&file, case["file"].as_str().unwrap()).unwrap();
        fs::set_permissions(&file, std::os::unix::fs::PermissionsExt::from_mode(0o600)).unwrap();
        assert_eq!(
            outcome(run_model::document(&dir, None, None)),
            expected(case),
            "{}",
            case["label"]
        );
        fs::remove_dir_all(dir).unwrap();
    }
}

#[test]
fn choose_matches_choose_run_model_for_every_oracle_trial() {
    let oracle = oracle();
    let cases = oracle["choose_cases"].as_array().unwrap();
    assert_eq!(cases.len(), 26);
    for (i, case) in cases.iter().enumerate() {
        let dir = root(&format!("choose{i}"));
        let dir_arg: &Path = if case["label"] == "write_failed" {
            Path::new("/nonexistent-l10-r7/state")
        } else {
            &dir
        };
        if case["label"] == "corrupt_state" {
            let file = dir.join(run_model::FILE);
            fs::write(&file, case["file"].as_str().unwrap()).unwrap();
            fs::set_permissions(&file, std::os::unix::fs::PermissionsExt::from_mode(0o600))
                .unwrap();
        } else if let Some(pre) = case["pre"].as_str() {
            run_model::save_choice(&dir, pre).unwrap();
        }
        let facts = Facts {
            env: case["env"].as_str(),
            acquisition_active: case["acquisition_active"] == json!(true),
            owner_run_active: case["owner_run_active"] == json!(true),
            device_memory_bytes: memory(case),
        };
        let body = case["body"].as_object().cloned().unwrap_or_default();
        let mut opened = 0;
        let got = run_model::choose(dir_arg, &facts, &body, &mut || opened += 1);
        assert_eq!(outcome(got), expected(case), "{}", case["label"]);
        assert_eq!(
            opened,
            case["opened"].as_u64().unwrap(),
            "{}",
            case["label"]
        );
        let file = dir.join(run_model::FILE);
        match case["file"].as_str() {
            Some(raw) => {
                assert_eq!(fs::read_to_string(&file).unwrap(), raw, "{}", case["label"]);
                assert_eq!(fs::metadata(&file).unwrap().mode() & 0o7777, 0o600);
            }
            None => assert!(!file.exists(), "{}", case["label"]),
        }
        let leftovers: Vec<_> = fs::read_dir(&dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .filter(|n| n != run_model::FILE)
            .collect();
        assert!(leftovers.is_empty(), "{} left {leftovers:?}", case["label"]);
        fs::remove_dir_all(dir).unwrap();
    }
}
