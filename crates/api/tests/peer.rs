// SPDX-License-Identifier: AGPL-3.0-only
//! `RemoteMacMember.accept_handoff`'s offline branches against the EXECUTED oracle
//! (`L10-evidence/r10-probe-peer-accept.py` -> `fixtures/oracle_peer_accept.json`, v0.1.4 f4e366e9):
//! retained acceptance, digest changes, rejoin replacement, the join checkpoint, and every invalid state
//! file, with the exception class the oracle raised and the files it left behind.
use api::peer::{Retained, accept_retained, load_membership_state};
use serde_json::{Map, Value, json};
use std::{fs, os::unix::fs::PermissionsExt, path::Path};

fn fixture() -> Value {
    let doc: Value =
        serde_json::from_str(include_str!("fixtures/oracle_peer_accept.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    doc
}
fn fresh(label: &str, files: &Value, modes: &Value) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("l10r10-peer-{}-{label}", std::process::id()));
    fs::remove_dir_all(&dir).unwrap_or(());
    fs::create_dir_all(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
    for (name, text) in files.as_object().unwrap() {
        fs::write(dir.join(name), text.as_str().unwrap()).unwrap();
        let mode = modes.get(name).and_then(Value::as_str).map_or(0o600, |m| {
            u32::from_str_radix(m.trim_start_matches("0o"), 8).unwrap()
        });
        fs::set_permissions(dir.join(name), fs::Permissions::from_mode(mode)).unwrap();
    }
    dir
}
fn listing(dir: &Path) -> Map<String, Value> {
    let mut out = Map::new();
    for entry in fs::read_dir(dir).unwrap() {
        let entry = entry.unwrap();
        if entry.file_type().unwrap().is_file() {
            let path = entry.path();
            out.insert(
                entry.file_name().to_string_lossy().into_owned(),
                json!({
                    "text": fs::read_to_string(&path).unwrap(),
                    "mode": format!("0o{:o}", fs::metadata(&path).unwrap().permissions().mode() & 0o777),
                }),
            );
        }
    }
    out
}

#[test]
fn offline_accept_branches_equal_the_oracle() {
    let doc = fixture();
    let cases = doc["cases"].as_array().unwrap();
    assert!(cases.len() >= 43, "{} cases", cases.len());
    let mut failures = Vec::new();
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let dir = fresh(label, &case["files_in"], &case["modes"]);
        let started = case["started"].as_bool().unwrap();
        let got = accept_retained(&dir, started, &case["handoff"]);
        let mut files = listing(&dir);
        let mut want_files = case["files"].as_object().unwrap().clone();
        let mut problems = Vec::new();
        match (&got, case.get("ok"), case.get("err")) {
            (Ok(Retained::Accepted(answer)), Some(ok), None) => {
                if &answer.to_value() != ok {
                    problems.push(format!("answer {} != {ok}", answer.to_value()));
                }
            }
            (Ok(Retained::Continue), None, Some(err)) => {
                // The source goes on to persist the handoff and then fails in the (not yet ported) seed
                // half; every one of these cases ends in `peer_pairing_state_invalid` there because no
                // pending request exists. The caller writes `pairing.handoff.json`.
                if err["code"] != "peer_pairing_state_invalid" {
                    problems.push(format!("continued but the oracle says {err}"));
                }
                files.remove("pairing.handoff.json");
                want_files.remove("pairing.handoff.json");
            }
            (Err(e), None, Some(err)) => {
                let (class, module) = e.raised.identity();
                if e.code != err["code"].as_str().unwrap() || e.status != 409 {
                    problems.push(format!("code {} != {}", e.code, err["code"]));
                }
                if (class, module)
                    != (
                        err["type"].as_str().unwrap(),
                        err["module"].as_str().unwrap(),
                    )
                {
                    problems.push(format!(
                        "raised {class}/{module} != {}/{}",
                        err["type"], err["module"]
                    ));
                }
            }
            other => problems.push(format!("outcome {other:?} vs {case}")),
        }
        if Value::Object(files.clone()) != Value::Object(want_files.clone()) {
            problems.push(format!(
                "files {:?} != {:?}",
                files.keys(),
                want_files.keys()
            ));
        }
        if !problems.is_empty() {
            failures.push(format!("{label}: {}", problems.join(" | ")));
        }
        fs::remove_dir_all(&dir).unwrap_or(());
    }
    assert!(
        failures.is_empty(),
        "{} of {} differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}

#[test]
fn load_membership_state_returns_the_retained_credential_or_none() {
    let doc = fixture();
    let ok = doc["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["label"] == "retained_same_digest")
        .unwrap();
    let dir = fresh("load", &ok["files_in"], &json!({}));
    let state = load_membership_state(&dir).unwrap().unwrap();
    assert_eq!(state["membership_generation"], 3);
    assert_eq!(state["node_id"], "remote-mac-0123456789abcdef01234567");
    fs::remove_file(dir.join("membership.state.json")).unwrap();
    assert_eq!(load_membership_state(&dir).unwrap(), None);
    fs::remove_dir_all(&dir).unwrap_or(());
}
