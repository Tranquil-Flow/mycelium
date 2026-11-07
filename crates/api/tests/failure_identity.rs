// SPDX-License-Identifier: AGPL-3.0-only
//! Ruling 2: `last-action-error.private.json` (a persisted user file) records the same
//! `exception_type` / `exception_module` v0.1.4 records for each failure path. Expected records
//! are the EXECUTED oracle's (`L10-evidence/r9-probe-failures.py` -> `fixtures/oracle_failures.json`:
//! `DemoApp.action` then `bounded_action_failure`), reached through the real preference files.
use api::{
    actions::{Action, Backend, Control},
    desktop::Service,
    pairing::SignedBackend,
    persistent::{Host, PersistentBackend},
    preferences::dumps_sorted,
    schema::PublicError,
};
use serde_json::{Map, Value};
use std::{fs, os::unix::fs::PermissionsExt, path::Path};

struct Plain {
    target: bool,
}
impl Host for Plain {
    fn closed(&self) -> bool {
        false
    }
    fn owner(&self) -> bool {
        !self.target
    }
    fn environment(&self, _: &str) -> Option<String> {
        None
    }
    fn device_memory_bytes(&self) -> Option<u64> {
        None
    }
    fn run_active(&self) -> bool {
        false
    }
    fn acquisition_active(&self) -> bool {
        false
    }
    fn owner_run_active(&self) -> bool {
        false
    }
    fn adopt_target_role(&mut self) -> Result<(), PublicError> {
        Ok(())
    }
    fn start_authority(&mut self) -> Result<(), PublicError> {
        Ok(())
    }
}
struct Nothing;
impl Backend for Nothing {
    fn prepare(&mut self, _: Action, _: &Map<String, Value>) -> Result<(), PublicError> {
        Ok(())
    }
    fn execute(&mut self, _: Action, _: &Map<String, Value>) -> Result<Value, PublicError> {
        Err(PublicError::new(404, "inner_must_not_serve_this"))
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        Ok(())
    }
}
fn hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
/// The probe's `setup` hooks, by name.
fn setup(name: &str, root: &Path) {
    let join = |n: &str| root.join(n);
    match name {
        "sym" => std::os::unix::fs::symlink("/nonexistent-r9-target", join("resource-policy.json"))
            .unwrap(),
        "unreadable" => {
            fs::write(join("resource-policy.json"), b"{}").unwrap();
            fs::set_permissions(
                join("resource-policy.json"),
                fs::Permissions::from_mode(0o000),
            )
            .unwrap();
        }
        "joindir" => {
            fs::create_dir(join("join-window.json")).unwrap();
            fs::write(join("join-window.json/x"), b"1").unwrap();
        }
        "ro_root" => fs::set_permissions(root, fs::Permissions::from_mode(0o500)).unwrap(),
        "policy_dir" => fs::create_dir(join("resource-policy.json")).unwrap(),
        "bad_mode" => {
            fs::write(join("resource-policy.json"), b"{}").unwrap();
            fs::set_permissions(
                join("resource-policy.json"),
                fs::Permissions::from_mode(0o644),
            )
            .unwrap();
        }
        other => panic!("unknown setup {other}"),
    }
}
/// Cases whose oracle answer is an artifact of the probe's stand-in application.
const STAND_IN_ARTIFACTS: [&str; 1] = ["run_model_write_readonly_root"];

#[test]
fn every_failure_records_the_oracle_exception_identity() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/oracle_failures.json")).unwrap();
    assert_eq!(
        fixture["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let cases = fixture["cases"].as_array().unwrap();
    assert!(cases.len() >= 22);
    let mut failures = Vec::new();
    for case in cases {
        let label = case["label"].as_str().unwrap();
        if STAND_IN_ARTIFACTS.contains(&label) {
            continue;
        }
        let dir = std::env::temp_dir().join(format!("l10-ident-{}-{label}", std::process::id()));
        fs::remove_dir_all(&dir).unwrap_or(());
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        for (name, raw) in case["files"].as_object().into_iter().flatten() {
            fs::write(dir.join(name), hex(raw.as_str().unwrap())).unwrap();
            fs::set_permissions(dir.join(name), fs::Permissions::from_mode(0o600)).unwrap();
        }
        if let Some(name) = case["setup"].as_str() {
            setup(name, &dir);
        }
        let target = case["opts"]["role"] == "target";
        // The launcher's own composition: the signed wrapper around the persistent backend.
        let control = Control::new(SignedBackend::new(
            PersistentBackend::new(dir.clone(), Plain { target }, Nothing),
            || 1_761_700_000.125,
        ));
        let got = control.action_json(
            case["path"].as_str().unwrap(),
            case["body"].as_object().unwrap(),
        );
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        let mut problems = Vec::new();
        match (&got, case.get("err")) {
            (Err(e), Some(want)) => {
                if (u64::from(e.status), e.code)
                    != (
                        want["status"].as_u64().unwrap(),
                        want["code"].as_str().unwrap(),
                    )
                {
                    problems.push(format!("error {e} != {want}"));
                }
            }
            (Ok(_), None) => {}
            (other, want) => problems.push(format!("outcome {other:?} vs oracle {want:?}")),
        }
        let mut record = fs::read_to_string(dir.join("last-action-error.private.json")).ok();
        if case["setup"] == "ro_root" {
            // The handler's own write of the record fails in a read-only state directory
            // (`except BaseException: pass`), so nothing is on disk; the probe computes the
            // record regardless. Once the directory is writable again, the identity the
            // backend would have recorded for this very error must be the oracle's.
            assert!(
                record.is_none(),
                "{label}: record written into a read-only root"
            );
            let mut backend = PersistentBackend::new(dir.clone(), Plain { target }, Nothing);
            let error = got.as_ref().expect_err("a refused write");
            backend.record_failure(case["path"].as_str().unwrap(), error);
            record = fs::read_to_string(dir.join("last-action-error.private.json")).ok();
        }
        match (case.get("last_action_error"), record) {
            (Some(want), Some(raw)) => {
                if raw != dumps_sorted(want) {
                    problems.push(format!("record {raw} != {}", dumps_sorted(want)));
                }
            }
            (None, None) => {}
            (want, got) => problems.push(format!("record presence: oracle {want:?} rust {got:?}")),
        }
        if !problems.is_empty() {
            failures.push(format!("{label}: {}", problems.join(" | ")));
        }
        fs::remove_dir_all(&dir).unwrap();
    }
    assert!(
        failures.is_empty(),
        "{} of {} differ:\n{}",
        failures.len(),
        cases.len(),
        failures
            .iter()
            .take(14)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}
