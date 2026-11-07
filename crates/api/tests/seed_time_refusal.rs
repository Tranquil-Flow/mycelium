// SPDX-License-Identifier: AGPL-3.0-only
//! A seed window refusal (`seed_http_seed_time_invalid`) names its numbers twice in v0.1.4
//! (`app.py:370-420`, `:2008-2031`): `seed_window` inside `last-action-error.private.json`, and one
//! `seed_time_refused` line on the daemon log. Expected record and line are the EXECUTED oracle's
//! (`L10-evidence/r11-probe-seed-time.py` -> `fixtures/oracle_seed_time.json`); the refusal reaches
//! `PersistentBackend::record_failure` exactly as the action handler passes it.
use api::{
    actions::{Action, Backend},
    persistent::{Host, PersistentBackend},
    preferences::dumps_sorted,
    schema::{PublicError, Raised, SeedWindow},
};
use serde_json::{Map, Value};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    sync::{Arc, Mutex},
};

struct Logging {
    lines: Arc<Mutex<Vec<String>>>,
}
impl Host for Logging {
    fn closed(&self) -> bool {
        false
    }
    fn owner(&self) -> bool {
        false
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
    fn daemon_log(&mut self, line: &str) {
        self.lines.lock().unwrap().push(line.to_owned());
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

/// The error the oracle's member raises for this record: `RemoteMacMemberError` with the code, and
/// the window numbers of its `__cause__` when the record names them.
fn error_of(record: &Value) -> PublicError {
    let code: &'static str = match record["error_code"].as_str() {
        Some("seed_http_seed_time_invalid") => "seed_http_seed_time_invalid",
        Some("peer_transport_unreachable") => "peer_transport_unreachable",
        other => panic!("unexpected oracle code {other:?}"),
    };
    let error = PublicError::new(409, code).raised_by(Raised::Peer);
    match record.get("seed_window") {
        Some(window) => {
            let number = |key: &str| window[key].as_f64();
            error.with_window(SeedWindow {
                now: number("now"),
                issued_at: number("issued_at"),
                expires_at: number("expires_at"),
            })
        }
        None => error,
    }
}

#[test]
fn a_seed_time_refusal_records_its_window_and_logs_the_event_like_the_oracle() {
    let doc: Value = serde_json::from_str(include_str!("fixtures/oracle_seed_time.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    let cases = doc["cases"].as_array().unwrap();
    assert!(cases.len() >= 9);
    let mut failures = Vec::new();
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let dir = std::env::temp_dir().join(format!("l10r11-time-{}-{label}", std::process::id()));
        fs::remove_dir_all(&dir).unwrap_or(());
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        let lines = Arc::new(Mutex::new(Vec::new()));
        let mut backend = PersistentBackend::new(
            dir.clone(),
            Logging {
                lines: lines.clone(),
            },
            Nothing,
        );
        backend.record_failure(case["path"].as_str().unwrap(), &error_of(&case["record"]));
        let raw = fs::read_to_string(dir.join("last-action-error.private.json")).unwrap();
        let mut problems = Vec::new();
        if raw != dumps_sorted(&case["record"]) {
            problems.push(format!("record {raw} != {}", dumps_sorted(&case["record"])));
        }
        let logged = lines.lock().unwrap().clone();
        let want: Vec<String> = case["event_line"]
            .as_str()
            .map(str::to_owned)
            .into_iter()
            .collect();
        if logged != want {
            problems.push(format!("daemon log {logged:?} != {want:?}"));
        }
        if !problems.is_empty() {
            failures.push(format!("{label}: {}", problems.join(" | ")));
        }
        fs::remove_dir_all(&dir).unwrap_or(());
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
