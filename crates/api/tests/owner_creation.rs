// SPDX-License-Identifier: AGPL-3.0-only
//! Owner creation prelude, the `adaptive_sharing` split-off and standing-consent withdrawal
//! through the HTTP action service, against the EXECUTED oracle (`fixtures/oracle_consent.json`,
//! `fixtures/oracle_adaptive.json`; `L10-evidence/r7-probe-consent.py`, `r7-probe-adaptive.py`):
//! `DemoApp._action('/api/create')`, `DemoApp.action` and
//! `DemoApp._target_join_action('/api/consent/standing/withdraw')` run on a recording stand-in
//! authority. The authority itself (swarm creation, pairing) is the host's `create_owner`.
use api::{
    actions::{Action, Backend, Control},
    desktop::Service,
    persistent::{Host, PersistentBackend},
    preferences::{resource_policy, standing_consent as sc},
    schema::PublicError,
};
use serde_json::{Map, Value, json};
use std::{
    fs,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

#[derive(Default)]
struct State {
    closed: bool,
    target: bool,
    relay_fails: bool,
    create: Option<Result<Value, (u16, &'static str)>>,
    stop_control_present: bool,
    log: Vec<String>,
    adaptive: Vec<bool>,
}
#[derive(Clone, Default)]
struct FakeHost(Arc<Mutex<State>>);
impl FakeHost {
    fn with<R>(&self, f: impl FnOnce(&mut State) -> R) -> R {
        f(&mut self.0.lock().unwrap())
    }
}
impl Host for FakeHost {
    fn closed(&self) -> bool {
        self.with(|s| s.closed)
    }
    fn owner(&self) -> bool {
        self.with(|s| !s.target)
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
    fn now_seconds(&self) -> f64 {
        1_761_700_000.125
    }
    fn stop_control(&mut self) {
        self.with(|s| {
            if s.stop_control_present {
                s.log.push("stop_control".into());
            }
        });
    }
    fn start_public_relay(&mut self, ack: &Value, seconds: &Value) -> Result<(), PublicError> {
        self.with(|s| {
            s.log.push("relay".into());
            assert_eq!((ack, seconds), (&json!(true), &json!(600)));
            if s.relay_fails {
                Err(PublicError::new(400, "relay_failed"))
            } else {
                Ok(())
            }
        })
    }
    fn abort_public_relay(&mut self) {
        self.with(|s| s.log.push("relay_closed".into()));
    }
    fn create_owner(&mut self, _: &Map<String, Value>) -> Result<Value, PublicError> {
        self.with(|s| {
            s.log.push("create".into());
            match s.create.clone().unwrap_or(Ok(json!({"accepted": true}))) {
                Ok(v) => Ok(v),
                Err((status, code)) => Err(PublicError::new(status, code)),
            }
        })
    }
    fn apply_first_consent_choice(&mut self, enabled: bool) {
        self.with(|s| {
            s.log.push("adaptive".into());
            s.adaptive.push(enabled);
        });
    }
}
struct Inner;
impl Backend for Inner {
    fn prepare(&mut self, _: Action, _: &Map<String, Value>) -> Result<(), PublicError> {
        Ok(())
    }
    fn execute(&mut self, _: Action, _: &Map<String, Value>) -> Result<Value, PublicError> {
        Ok(json!({"inner": true}))
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        Ok(())
    }
}
fn oracle() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_consent.json")).unwrap()
}
fn root(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("l10-owner-{tag}-{}", std::process::id()));
    if dir.exists() {
        fs::remove_dir_all(&dir).unwrap();
    }
    fs::create_dir_all(&dir).unwrap();
    dir
}
fn body(v: &Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap_or_default()
}
fn outcome(r: Result<Value, PublicError>) -> Value {
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
fn app(dir: &Path, host: FakeHost) -> Control<PersistentBackend<FakeHost, Inner>> {
    Control::new(PersistentBackend::new(dir.to_path_buf(), host, Inner))
}
fn put(path: &Path, raw: &str) {
    fs::write(path, raw).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
}
/// The oracle's stand-in log reduced to what the desktop host observes.
fn project(log: &Value) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for entry in log.as_array().unwrap() {
        let name = match entry.as_str().unwrap_or_else(|| entry[0].as_str().unwrap()) {
            "relay" => "relay",
            "relay_closed" => "relay_closed",
            "bind" => continue,
            _ => "create",
        };
        if out.last().map(String::as_str) != Some(name) || name != "create" {
            out.push(name.to_string());
        }
    }
    out
}

#[test]
fn create_prelude_matches_the_oracle_for_every_case() {
    let oracle = oracle();
    let cases = oracle["create_cases"].as_array().unwrap();
    assert_eq!(cases.len(), 21);
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let dir = root(&format!("create-{label}"));
        let file = dir.join(resource_policy::FILE);
        if let Some(pre) = case["pre"].as_str() {
            put(&file, pre);
        }
        let host = FakeHost::default();
        host.with(|s| {
            s.closed = case["opts"]["closed"] == true;
            s.target = case["opts"]["role"] == "target";
            s.relay_fails = case["opts"]["relay_fail"] == true;
            s.create = match (label, case["opts"]["create_fail"] == true) {
                (_, true) => Some(Err((409, "action_not_completed"))),
                ("already_current", _) => Some(Ok(case["ok"].clone())),
                ("cannot_reenroll", _) => Some(Err((400, "membership_cannot_reenroll"))),
                _ => None,
            };
        });
        let target_dir = if label == "root_missing" {
            PathBuf::from("/nonexistent-l10-r7/state")
        } else {
            dir.clone()
        };
        let c = app(&target_dir, host.clone());
        let got = c.action("/api/create", &body(&case["body"]));
        assert_eq!(outcome(got), expected(case), "{label}");
        assert_eq!(
            host.with(|s| s.log.clone()),
            project(&case["log"]),
            "{label}"
        );
        if label != "root_missing" {
            match case["file"].as_str() {
                Some(raw) => {
                    assert_eq!(fs::read_to_string(&file).unwrap(), raw, "{label}");
                    assert_eq!(fs::metadata(&file).unwrap().mode() & 0o7777, 0o600);
                }
                None => assert!(!file.exists(), "{label}"),
            }
            // The oracle ran `DemoApp._action` directly; the HTTP handler additionally records a
            // non-DemoError (409) failure in `last-action-error.private.json`
            // (app.py:2008-2031), which `Control` now does. Nothing else may be left behind.
            let record = "last-action-error.private.json";
            let left: Vec<String> = fs::read_dir(&dir)
                .unwrap()
                .map(|e| e.unwrap().file_name().into_string().unwrap())
                .filter(|n| n != resource_policy::FILE && n != record)
                .collect();
            assert!(left.is_empty(), "{label}: {left:?}");
            let non_demo_failure = case["err"]["status"] == 409;
            assert_eq!(
                dir.join(record).exists(),
                non_demo_failure,
                "{label}: failure record"
            );
        }
        fs::remove_dir_all(dir).unwrap();
    }
}

#[test]
fn adaptive_sharing_is_split_off_before_the_strict_checks_and_applied_after_success() {
    let adaptive: Value =
        serde_json::from_str(include_str!("fixtures/oracle_adaptive.json")).unwrap();
    let cases = adaptive.as_array().unwrap();
    assert_eq!(cases.len(), 9);
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let dir = root(&format!("adaptive-{label}"));
        let host = FakeHost::default();
        host.with(|s| {
            s.closed = label.ends_with("_closed");
            if label == "create_true_create_fails" {
                s.create = Some(Err((409, "action_not_completed")));
            }
        });
        let c = app(&dir, host.clone());
        let got = c.action(case["path"].as_str().unwrap(), &body(&case["body"]));
        assert_eq!(outcome(got), expected(case), "{label}");
        let want_choice = match label {
            "create_true" => Some(true),
            "create_false" => Some(false),
            _ => None,
        };
        assert_eq!(
            host.with(|s| s.adaptive.clone()),
            want_choice.into_iter().collect::<Vec<_>>(),
            "{label}"
        );
        // The policy file the oracle wrote on success belongs to adaptive_host; the consent file
        // never carries the split-off key.
        let policy = dir.join(resource_policy::FILE);
        if policy.exists() {
            assert!(
                !fs::read_to_string(&policy).unwrap().contains("adaptive"),
                "{label}"
            );
        }
        fs::remove_dir_all(dir).unwrap();
    }
}

#[test]
fn withdraw_matches_the_oracle_route() {
    let oracle = oracle();
    let policy = oracle["consent_valid"][0]["consent"].clone();
    let caps = json!({"storage_bytes":1_000_000_000,"max_memory_bytes":4u64*1024*1024*1024,"max_wall_seconds":900,"max_commands":50});
    let grant = json!({"acknowledged":true,"disclosure_digest":sc::DISCLOSURE_DIGEST,"caps":caps});
    let cases = oracle["withdraw_cases"].as_array().unwrap();
    assert_eq!(cases.len(), 10);
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let dir = root(&format!("withdraw-{label}"));
        if let Some(pre) = case["pre"].as_str() {
            sc::grant(&dir, &grant, &policy, 1_761_600_000.5, None).unwrap();
            if pre == "withdrawn" {
                sc::withdraw(&dir, 1_761_600_001.0).unwrap();
            }
        }
        let host = FakeHost::default();
        host.with(|s| {
            s.target = case["role"] == "target";
            s.stop_control_present =
                label.contains("stop") || label == "withdraw_owner_with_authority";
        });
        let c = app(&dir, host.clone());
        let got = c.action("/api/consent/standing/withdraw", &body(&case["body"]));
        assert_eq!(outcome(got), expected(case), "{label}");
        let stops = host.with(|s| s.log.iter().filter(|l| *l == "stop_control").count());
        assert_eq!(stops as u64, case["stopped"].as_u64().unwrap(), "{label}");
        let file = dir.join(sc::FILE);
        match case["file"].as_str() {
            Some(raw) => assert_eq!(fs::read_to_string(&file).unwrap(), raw, "{label}"),
            None => assert!(!file.exists(), "{label}"),
        }
        fs::remove_dir_all(dir).unwrap();
    }
}

/// Lane A, release blocker B8 (GRT; ledger row 61 F1): the owner's own stage runs under its
/// local contributor's standing grant (`local-contributor/`), so the app's withdraw action on an
/// owner withdraws THAT grant too, as v0.1.4's owner withdrew the one grant its own node ran
/// under. An owner with no grant anywhere is still refused `standing_consent_missing` (oracle).
#[test]
fn an_owners_withdraw_reaches_its_contributors_grant() {
    let oracle = oracle();
    let policy = oracle["consent_valid"][0]["consent"].clone();
    let caps = json!({"storage_bytes":1_000_000_000,"max_memory_bytes":4u64*1024*1024*1024,"max_wall_seconds":900,"max_commands":50});
    let grant = json!({"acknowledged":true,"disclosure_digest":sc::DISCLOSURE_DIGEST,"caps":caps});
    let dir = root("withdraw-contributor");
    let contributor = api::owner_ports::contributor_dir(&dir);
    fs::create_dir_all(&contributor).unwrap();
    sc::grant(&contributor, &grant, &policy, 1_761_600_000.5, None).unwrap();
    let host = FakeHost::default();
    let c = app(&dir, host);
    let got = outcome(c.action(
        "/api/consent/standing/withdraw",
        &body(&json!({"confirm": true})),
    ));
    assert_eq!(got["ok"]["state"], "withdrawn", "{got}");
    assert_eq!(
        sc::load(&contributor).unwrap().unwrap()["state"],
        "withdrawn",
        "the contributor's grant is withdrawn"
    );
    assert!(!dir.join(sc::FILE).exists(), "nothing written at the owner's root");
    fs::remove_dir_all(dir).unwrap();
}
