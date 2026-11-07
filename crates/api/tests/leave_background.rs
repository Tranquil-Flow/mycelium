// SPDX-License-Identifier: AGPL-3.0-only
//! A SMOOTH member leave against the EXECUTED oracle's timing (`fixtures/oracle_leave_race.json`,
//! `L10-evidence/r10-probe-leave-race.py`, v0.1.4 f4e366e9): the action answers at once with the
//! persisted `begin` record, the drain runs on a daemon thread named `member-leave`, the fence is
//! raised before the answer, and a second request while the drain runs only reports the status.
use api::{
    actions::{Action, Backend, Control},
    desktop::Service,
    leave::{LeaveWorker, Limits, MemberLeave, Unobservable, status_view},
    pairing::SignedBackend,
    persistent::{Host, PersistentBackend},
    schema::PublicError,
};
use budgets::BudgetId;
use journey_tests::duration;
use serde_json::{Map, Value, json};
use std::{
    os::unix::fs::PermissionsExt,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

/// How long the work stays in flight (the probe patched `inflight_count` for 0.8 s; the answer must come
/// well before any such drain). The member-leave poll period (0.5 s) is the registry's `OwnerPoll`.
fn work() -> Duration {
    duration(BudgetId::OwnerPoll).unwrap()
}
fn tick() -> Duration {
    duration(BudgetId::PyMyceliumA6ProductFaultSeamsLiveproductfaulttargetInitPollSecondsL193)
        .unwrap()
}
fn ceiling() -> Duration {
    duration(BudgetId::WorkerStartup).unwrap()
}
/// Work that stays in flight for `work()`, then drains.
#[derive(Default)]
struct Seen {
    fenced: bool,
    threads: Vec<String>,
    inflight_calls: u32,
    steps: Vec<String>,
}
struct SlowHost {
    seen: Arc<Mutex<Seen>>,
    started: Instant,
}
impl Host for SlowHost {
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
    fn now_seconds(&self) -> f64 {
        1_761_700_000.25
    }
    fn leave_worker(&mut self) -> Box<dyn LeaveWorker> {
        Box::new(SlowWorker {
            seen: self.seen.clone(),
            started: self.started,
        })
    }
    fn leave_mark(&mut self, fence: bool, _left: bool) {
        self.seen.lock().unwrap().fenced |= fence;
    }
}
struct SlowWorker {
    seen: Arc<Mutex<Seen>>,
    started: Instant,
}
impl LeaveWorker for SlowWorker {
    fn step(&mut self, name: &str) -> Result<(), Option<String>> {
        self.seen.lock().unwrap().steps.push(name.to_owned());
        Ok(())
    }
    fn inflight(&mut self) -> Result<i64, Unobservable> {
        let mut seen = self.seen.lock().unwrap();
        seen.inflight_calls += 1;
        seen.threads.push(
            std::thread::current()
                .name()
                .unwrap_or("<unnamed>")
                .to_owned(),
        );
        Ok(i64::from(self.started.elapsed() < work()))
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
fn body(mode: &str) -> Map<String, Value> {
    match json!({"mode": mode, "confirm": true}) {
        Value::Object(map) => map,
        _ => Map::new(),
    }
}
fn oracle() -> Value {
    let doc: Value = serde_json::from_str(include_str!("fixtures/oracle_leave_race.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    doc
}
fn state_of(dir: &std::path::Path) -> (String, Vec<String>) {
    let leave = MemberLeave::new(dir, Limits::default());
    let view = status_view(&leave).unwrap();
    (
        view["state"].as_str().unwrap_or("").to_owned(),
        view["steps"]
            .as_array()
            .map(|a| {
                a.iter()
                    .map(|s| s["name"].as_str().unwrap_or("").to_owned())
                    .collect()
            })
            .unwrap_or_default(),
    )
}
type Svc = Control<SignedBackend<PersistentBackend<SlowHost, Nothing>, fn() -> f64>>;
fn setup(label: &str) -> (Svc, std::path::PathBuf, Arc<Mutex<Seen>>) {
    setup_aged(label, Duration::ZERO)
}
/// `age`: how long the host's work has already been running (past `work()` = nothing in flight).
fn setup_aged(label: &str, age: Duration) -> (Svc, std::path::PathBuf, Arc<Mutex<Seen>>) {
    let dir = std::env::temp_dir().join(format!("l10r10-{label}-{}", std::process::id()));
    std::fs::remove_dir_all(&dir).unwrap_or(());
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    let seen = Arc::new(Mutex::new(Seen::default()));
    let host = SlowHost {
        seen: seen.clone(),
        started: Instant::now().checked_sub(age).unwrap_or_else(Instant::now),
    };
    let control = Control::new(SignedBackend::new(
        PersistentBackend::new(dir.clone(), host, Nothing),
        (|| 1_761_700_000.25) as fn() -> f64,
    ));
    (control, dir, seen)
}
fn wait_left(dir: &std::path::Path) -> Duration {
    let started = Instant::now();
    while started.elapsed() < ceiling() {
        if state_of(dir).0 == "left" {
            return started.elapsed();
        }
        std::thread::sleep(tick());
    }
    panic!("the background leave never reached `left`");
}

#[test]
fn no_work_in_flight_answers_with_the_begin_record_like_all_300_oracle_runs() {
    let doc = oracle();
    let histogram = doc["no_work_histogram"].as_array().unwrap();
    assert_eq!(histogram.len(), 1);
    assert_eq!(histogram[0]["count"], 300);
    for n in 0..30 {
        let (control, dir, _seen) = setup_aged(&format!("idle{n}"), ceiling());
        let answer = control.action("/api/leave", &body("smooth")).unwrap();
        assert_eq!(answer["state"], histogram[0]["state"], "run {n}");
        assert_eq!(answer["steps"], histogram[0]["steps"], "run {n}");
        wait_left(&dir);
        std::fs::remove_dir_all(&dir).unwrap_or(());
    }
}

#[test]
fn smooth_leave_answers_at_once_and_drains_on_a_member_leave_thread() {
    let doc = oracle();
    let want = &doc["in_flight"];
    assert_eq!(want["thread_name"], "member-leave");
    assert_eq!(want["response_seconds_lt_0_1"], true);
    let (control, dir, seen) = setup("inflight");
    let started = Instant::now();
    let answer = control.action("/api/leave", &body("smooth")).unwrap();
    let responded = started.elapsed();
    // The oracle answers in well under 0.1 s; allow scheduler noise but never the drain.
    assert!(responded < work() / 2, "answered after {responded:?}");
    assert_eq!(answer, want["answer"]);
    assert!(
        seen.lock().unwrap().fenced,
        "the fence is raised before the answer"
    );
    let finished = wait_left(&dir);
    assert!(
        started.elapsed() >= work(),
        "left before the work drained ({:?}, waited {finished:?})",
        started.elapsed()
    );
    let (state, steps) = state_of(&dir);
    assert_eq!(state, want["final_state"]);
    assert_eq!(json!(steps), want["final_steps"]);
    let seen = seen.lock().unwrap();
    assert!(seen.inflight_calls >= 2, "polled while the work ran");
    assert!(
        seen.threads.iter().all(|t| t == "member-leave"),
        "the drain ran on {:?}",
        seen.threads
    );
    std::fs::remove_dir_all(&dir).unwrap_or(());
}

#[test]
fn a_second_request_while_the_drain_runs_only_reports_the_status() {
    let (control, dir, seen) = setup("second");
    let first = control.action("/api/leave", &body("smooth")).unwrap();
    let second = control.action("/api/leave", &body("smooth")).unwrap();
    let instant = control.action("/api/leave", &body("instant")).unwrap();
    // v0.1.4 (`second_while_running`): both answer with the running leave's persisted status.
    assert_eq!(first["mode"], "smooth");
    assert_eq!(second["mode"], "smooth");
    assert_eq!(
        instant["mode"], "smooth",
        "instant must not start while a smooth leave runs"
    );
    assert_eq!(second["state"], "draining");
    wait_left(&dir);
    // Only one leave ever ran: one `fence` step.
    let fences = seen
        .lock()
        .unwrap()
        .steps
        .iter()
        .filter(|n| *n == "fence")
        .count();
    assert_eq!(fences, 1);
    std::fs::remove_dir_all(&dir).unwrap_or(());
}
