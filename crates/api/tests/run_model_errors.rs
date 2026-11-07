// SPDX-License-Identifier: AGPL-3.0-only
//! Integrator ruling (b): `run_model::save_choice/load_choice/choice` must not leak internal
//! `ValueError` messages under 409. Expected values are the EXECUTED oracle's exceptions mapped
//! as the HTTP handler maps them (`r8-probe-run-model-errors.py`, app.py:2003-2031).
use api::preferences::run_model;
use serde_json::Value;
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};

fn oracle() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_run_model_errors.json")).unwrap()
}
fn root(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("l10-rme-{tag}-{}", std::process::id()));
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
fn wire<T>(r: Result<T, api::schema::PublicError>) -> Option<(u16, &'static str)> {
    r.err().map(|e| (e.status, e.code))
}

#[test]
fn save_choice_errors_are_the_collapsed_wire_code() {
    let oracle = oracle();
    for case in oracle["save"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let dir = root("save");
        let got = wire(run_model::save_choice(&dir, name));
        let want = case["err"].as_object().map(|e| {
            (
                e["status"].as_u64().unwrap() as u16,
                e["code"].as_str().unwrap().to_owned(),
            )
        });
        assert_eq!(got.map(|(s, c)| (s, c.to_owned())), want, "save {name:?}");
        fs::remove_dir_all(dir).unwrap();
    }
}
#[test]
fn choice_errors_are_the_collapsed_wire_code() {
    assert_eq!(
        wire(run_model::choice("gpt")),
        Some((409, "action_not_completed"))
    );
    assert!(run_model::choice("qwen3-8b").is_ok());
}
#[test]
fn load_choice_errors_are_the_collapsed_wire_code() {
    let oracle = oracle();
    for case in oracle["load"].as_array().unwrap() {
        let label = case["label"].as_str().unwrap();
        let dir = root("load");
        let file = dir.join(run_model::FILE);
        let raw: Option<&[u8]> = match label {
            "unknown_choice" => {
                Some(br#"{"choice":"zzz","protocol":"mycelium.a13_run_model_choice.v1"}"#)
            }
            "extra_key" => Some(
                br#"{"choice":"qwen3-8b","extra":1,"protocol":"mycelium.a13_run_model_choice.v1"}"#,
            ),
            "wrong_protocol" => {
                Some(br#"{"choice":"qwen3-8b","protocol":"mycelium.a13_fast_mode.v1"}"#)
            }
            "not_json" => Some(b"zzz"),
            "valid" => {
                Some(br#"{"choice":"qwen3-8b","protocol":"mycelium.a13_run_model_choice.v1"}"#)
            }
            "mode_0644" => {
                put(
                    &file,
                    br#"{"choice":"qwen3-8b","protocol":"mycelium.a13_run_model_choice.v1"}"#,
                    0o644,
                );
                None
            }
            "directory" => {
                fs::create_dir(&file).unwrap();
                None
            }
            other => panic!("unmapped oracle case {other}"),
        };
        if let Some(raw) = raw {
            put(&file, raw, 0o600);
        }
        let got = run_model::load_choice(&dir);
        match case.get("err") {
            Some(err) => {
                let e = got.unwrap_err();
                assert_eq!(
                    (u64::from(e.status), e.code),
                    (
                        err["status"].as_u64().unwrap(),
                        err["code"].as_str().unwrap()
                    ),
                    "load {label}"
                );
            }
            None => assert_eq!(got.unwrap(), case["ok"].as_str(), "load {label}"),
        }
        fs::remove_dir_all(dir).unwrap();
    }
}
