// SPDX-License-Identifier: AGPL-3.0-only
//! Member leave against the EXECUTED oracle (`L10-evidence/r9-probe-leave.py` ->
//! `fixtures/oracle_leave.json`: the real `leave_action`, `MemberLeave`, `leave_status` and
//! `resume_leave` over an unjoined target, plus the bounded drain with injected clock/sleeps).
//! A SMOOTH leave answers at once with the persisted `begin` record (`draining`, no steps) and drains
//! on a background thread, exactly as the oracle does (300/300 executed answers:
//! `fixtures/oracle_leave_race.json`); the probe's `thread.join` becomes `settle`.
use api::{
    actions::{Action, Backend, Control},
    desktop::Service,
    leave::{
        Clock, LeaveWorker, Limits, MemberLeave, Steps, Unobservable, resume_leave, status_view,
    },
    pairing::SignedBackend,
    persistent::{Host, PersistentBackend},
    schema::PublicError,
};
use budgets::BudgetId;
use journey_tests::duration;
use serde_json::{Map, Value, json};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::Path,
    sync::{Arc, Mutex},
};

#[derive(Default)]
struct Flags {
    fence: bool,
    left: bool,
    cancelled: u32,
}
struct RecHost {
    target: bool,
    flags: Arc<Mutex<Flags>>,
}
impl Host for RecHost {
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
    fn now_seconds(&self) -> f64 {
        1_761_700_000.25
    }
    fn leave_worker(&mut self) -> Box<dyn LeaveWorker> {
        Box::new(RecWorker(self.flags.clone()))
    }
    fn leave_mark(&mut self, fence: bool, left: bool) {
        let mut flags = self.flags.lock().unwrap();
        flags.fence |= fence;
        flags.left |= left;
    }
}
struct RecWorker(Arc<Mutex<Flags>>);
impl LeaveWorker for RecWorker {
    fn step(&mut self, name: &str) -> Result<(), Option<String>> {
        let mut flags = self.0.lock().unwrap();
        match name {
            "fence" => flags.fence = true,
            "disconnect" => flags.left = true,
            "cancel_work" => flags.cancelled += 1,
            _ => {}
        }
        Ok(())
    }
    fn now_seconds(&self) -> f64 {
        1_761_700_000.25
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
struct Recorder(Arc<Mutex<Flags>>);
impl Steps for Recorder {
    fn step(&mut self, name: &str) -> Result<(), Option<String>> {
        let mut flags = self.0.lock().unwrap();
        match name {
            "fence" => flags.fence = true,
            "disconnect" => flags.left = true,
            "cancel_work" => flags.cancelled += 1,
            _ => {}
        }
        Ok(())
    }
    fn mark(&mut self, fence: bool, left: bool) {
        let mut flags = self.0.lock().unwrap();
        flags.fence |= fence;
        flags.left |= left;
    }
}
struct Fixed;
impl Clock for Fixed {
    fn monotonic(&mut self) -> f64 {
        0.0
    }
    fn sleep(&mut self, _: f64) {}
    fn wall(&mut self) -> f64 {
        1_761_700_000.25
    }
}
/// The probe joins `app._leave_thread` before it reads anything: wait (bounded) until the
/// background leave has persisted its final `left` record, if one is running.
fn settle(leave: &MemberLeave) {
    let started = std::time::Instant::now();
    while started.elapsed() < duration(BudgetId::WorkerStartup).unwrap() {
        let state = status_view(leave)
            .ok()
            .and_then(|v| v["state"].as_str().map(str::to_owned));
        if state.as_deref() != Some("draining") && state.as_deref() != Some("withdrawing") {
            return;
        }
        std::thread::sleep(
            duration(
                BudgetId::PyMyceliumA6ProductFaultSeamsLiveproductfaulttargetInitPollSecondsL193,
            )
            .unwrap(),
        );
    }
}
#[test]
fn completed_membership_leave_retains_failed_cleanup_for_resume_only() {
    struct Debt {
        calls: Vec<String>,
        refused: bool,
    }
    impl Steps for Debt {
        fn step(&mut self, name: &str) -> Result<(), Option<String>> {
            self.calls.push(name.into());
            if name == "cancel_work" && self.refused {
                Err(Some("helper_reap_unproven".into()))
            } else {
                Ok(())
            }
        }
    }
    let dir = fresh("cleanup-debt", &json!({}));
    let leave = MemberLeave::new(&dir, Limits::default());
    let mut debt = Debt {
        calls: Vec::new(),
        refused: true,
    };
    let record = leave.run("instant", &mut debt, &mut Fixed).unwrap();
    assert_eq!(
        record["state"], "left",
        "privacy departure is independent of cleanup"
    );
    assert_eq!(
        debt.calls,
        ["fence", "withdraw_consent", "disconnect", "cancel_work"]
    );
    debt.calls.clear();
    // Reopen the durable record, as a restart does, without inventing a new leave.
    let reopened = MemberLeave::new(&dir, Limits::default());
    let failed = reopened.resume(&mut debt, &mut Fixed).unwrap();
    assert!(
        failed.is_some(),
        "left must not discard failed cleanup debt"
    );
    assert_eq!(debt.calls, ["cancel_work"]);
    debt.calls.clear();
    debt.refused = false;
    let completed = reopened.resume(&mut debt, &mut Fixed).unwrap().unwrap();
    assert_eq!(debt.calls, ["cancel_work"]);
    assert_eq!(completed["started_at"], record["started_at"]);
    assert_eq!(completed["steps"].as_array().unwrap().len(), 4);
    assert!(
        completed["steps"]
            .as_array()
            .unwrap()
            .iter()
            .all(|row| row["ok"] == true)
    );
    debt.calls.clear();
    assert!(reopened.resume(&mut debt, &mut Fixed).unwrap().is_none());
    assert!(debt.calls.is_empty());
    fs::remove_dir_all(dir).unwrap();
}

fn fixture() -> Value {
    let fixture: Value = serde_json::from_str(include_str!("fixtures/oracle_leave.json")).unwrap();
    assert_eq!(
        fixture["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    fixture
}
fn fresh(label: &str, files: &Value) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("l10-leave-{}-{label}", std::process::id()));
    fs::remove_dir_all(&dir).unwrap_or(());
    fs::create_dir_all(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
    for (name, text) in files.as_object().unwrap() {
        fs::write(dir.join(name), text.as_str().unwrap()).unwrap();
        // pathlib.write_text under the probe's umask 022
        fs::set_permissions(dir.join(name), fs::Permissions::from_mode(0o644)).unwrap();
    }
    dir
}
fn listing(dir: &Path) -> Value {
    let mut out = Map::new();
    let mut names: Vec<_> = fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    names.sort();
    for name in names {
        let path = dir.join(&name);
        let mode = format!(
            "0o{:o}",
            fs::metadata(&path).unwrap().permissions().mode() & 0o777
        );
        out.insert(
            name,
            json!({"text": fs::read_to_string(&path).unwrap(), "mode": mode}),
        );
    }
    Value::Object(out)
}
fn view(leave: &MemberLeave) -> Value {
    match status_view(leave) {
        Ok(v) => json!({"ok": v}),
        Err(_) => json!({"err": {"type": "TypeError", "module": "builtins", "code": null}}),
    }
}
/// The oracle's error record without Python's free-text `message` (only the class matters).
fn plain(mut value: Value) -> Value {
    match &mut value {
        Value::Object(map) => {
            if map.get("type") == Some(&Value::from("TypeError")) {
                map.remove("message");
            }
            for v in map.values_mut() {
                *v = plain(v.take());
            }
        }
        Value::Array(items) => items.iter_mut().for_each(|v| *v = plain(v.take())),
        _ => {}
    }
    value
}

#[test]
fn leave_actions_resume_and_state_files_equal_the_oracle() {
    let doc = fixture();
    let cases = doc["cases"].as_array().unwrap();
    assert!(cases.len() >= 44);
    let mut failures = Vec::new();
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let dir = fresh(label, &case["files_in"]);
        let flags = Arc::new(Mutex::new(Flags::default()));
        let target = case["role"] == "target";
        let leave = MemberLeave::new(&dir, Limits::default());
        let mut problems = Vec::new();
        if case.get("resume").is_some() {
            let got = resume_leave(&leave, &mut Recorder(flags.clone()), &mut Fixed);
            let want = &case["resume"];
            match (&got, want.get("ok"), want.get("err")) {
                (Ok(None), Some(Value::Null), _) => {}
                (Ok(Some(_)), Some(ok), _) if !ok.is_null() => {}
                (Err(_), None, Some(_)) => {}
                other => problems.push(format!("resume {other:?} vs {want}")),
            }
            let after = &case["after_resume"];
            let seen = json!({"fence": flags.lock().unwrap().fence, "left": flags.lock().unwrap().left, "status": view(&leave)});
            if seen != plain(after.clone()) {
                problems.push(format!("after_resume {seen} != {after}"));
            }
        }
        let control = Control::new(SignedBackend::new(
            PersistentBackend::new(
                dir.clone(),
                RecHost {
                    target,
                    flags: flags.clone(),
                },
                Nothing,
            ),
            || 1_761_700_000.25,
        ));
        for (n, step) in case["steps"].as_array().unwrap().iter().enumerate() {
            let body = match &step["body"] {
                Value::Object(m) => m.clone(),
                // Non-object bodies never reach an action (HTTP ingress refuses them).
                _ => continue,
            };
            let got = control.action("/api/leave", &body);
            // The oracle's own answer, also for a smooth leave: the persisted `begin` record.
            let want_answer = step.get("ok").cloned().unwrap_or(Value::Null);
            match (&got, step.get("err")) {
                (Ok(value), None) if value == &want_answer => {}
                (Err(e), Some(want))
                    if want["message"].as_str().unwrap_or("") == e.code && e.status == 400 => {}
                other => problems.push(format!("step {n}: {other:?} vs {step}")),
            }
            settle(&leave);
            let after = json!({"fence": flags.lock().unwrap().fence, "left": flags.lock().unwrap().left, "cancelled": flags.lock().unwrap().cancelled});
            let want_after = json!({"fence": step["fence"], "left": step["left"], "cancelled": step["cancelled"]});
            if after != want_after {
                problems.push(format!("step {n} effects {after} != {want_after}"));
            }
            if view(&leave) != plain(step["status"].clone()) {
                problems.push(format!(
                    "step {n} status {} != {}",
                    view(&leave),
                    step["status"]
                ));
            }
        }
        if listing(&dir) != case["files"] {
            problems.push(format!("files {} != {}", listing(&dir), case["files"]));
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
        failures
            .iter()
            .take(12)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}

/// The bounded drain: a scripted `inflight()` and a virtual clock; the record, file and the number
/// of sleeps must be the oracle's.
struct Script {
    counts: Vec<i64>,
    calls: usize,
    raise_at: Option<usize>,
    handoff: Option<bool>,
}
impl Steps for Script {
    fn inflight(&mut self) -> Result<i64, Unobservable> {
        self.calls += 1;
        if self.raise_at == Some(self.calls) {
            return Err(Unobservable);
        }
        Ok(self.counts[(self.calls - 1).min(self.counts.len() - 1)])
    }
    fn handoff(&mut self) -> Option<Result<bool, Unobservable>> {
        self.handoff.map(Ok)
    }
}
struct Virtual {
    now: f64,
    sleeps: usize,
}
impl Clock for Virtual {
    fn monotonic(&mut self) -> f64 {
        self.now
    }
    fn sleep(&mut self, seconds: f64) {
        self.sleeps += 1;
        self.now += seconds;
    }
    fn wall(&mut self) -> f64 {
        1_761_700_000.25
    }
}
#[test]
fn smooth_drain_stall_cap_and_handoff_equal_the_oracle() {
    let doc = fixture();
    let drains = doc["drains"].as_array().unwrap();
    assert_eq!(drains.len(), 9);
    for case in drains {
        let label = case["label"].as_str().unwrap();
        let dir = fresh(label, &json!({}));
        let limits = Limits {
            stall: case["stall"].as_f64().unwrap(),
            cap: case["cap"].as_f64().unwrap(),
            poll: case["poll"].as_f64().unwrap(),
        };
        let leave = MemberLeave::new(&dir, limits);
        let mut steps = Script {
            counts: case["counts"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_i64().unwrap())
                .collect(),
            calls: 0,
            raise_at: case["raise_at"].as_u64().map(|n| n as usize),
            handoff: case["handoff"].as_bool(),
        };
        let mut clock = Virtual {
            now: 0.0,
            sleeps: 0,
        };
        let record = leave.run("smooth", &mut steps, &mut clock).unwrap();
        assert_eq!(Value::Object(record), case["record"], "{label} record");
        assert_eq!(clock.sleeps as u64, case["sleeps"], "{label} sleeps");
        assert_eq!(
            clock.now,
            case["elapsed"].as_f64().unwrap(),
            "{label} elapsed"
        );
        assert_eq!(
            steps.calls as u64, case["inflight_calls"],
            "{label} inflight calls"
        );
        assert_eq!(
            fs::read_to_string(dir.join("leave-state.json")).unwrap(),
            case["file"].as_str().unwrap(),
            "{label} file"
        );
        fs::remove_dir_all(&dir).unwrap_or(());
    }
}
