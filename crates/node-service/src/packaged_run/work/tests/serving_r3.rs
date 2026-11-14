// SPDX-License-Identifier: AGPL-3.0-only
//! Lane SRV round 3 on the real packaged adapter (REVIEW-laneSRV-r2-astra R2A-02 =
//! REVIEW-laneSRV-r2-opus R2-3): Quit during the serving step ends the run `stopped`, never
//! `failed` on the workflow Stop it issued itself. Model-free: no native, no route.
use super::*;
use api::actions::Action;
use owner_run::action::DemoFacts;
use owner_run::steps::exchange::{Announcement, ClockSkew, MemberRow, ReportsPort};
use owner_run::steps::publish::{ServingIdentity, WorkPort};
use owner_run::steps::serving::run_serving_step;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;

pub(super) struct Facts;
impl DemoFacts for Facts {
    fn owner(&self) -> bool {
        true
    }
    fn ensure_exchange(&mut self) -> bool {
        true
    }
    fn members_joined_without_relay(&self) -> Option<u64> {
        Some(0)
    }
}

/// `run.join(60)`'s allowance (demo_app.py:203): the longest any wait here may take.
pub(super) fn patience() -> Duration {
    span(BudgetId::PyMyceliumA13MacosDemoAppCloseRunJoinL203).unwrap()
}

/// Waits for a release; a dropped sender (the test unwound) releases as well.
pub(super) fn hold(release: &mpsc::Receiver<()>) {
    let timed_out = matches!(
        release.recv_timeout(patience()),
        Err(mpsc::RecvTimeoutError::Timeout)
    );
    assert!(!timed_out, "test release timed out");
}

/// No members and no reports: the serving wait's report reads are empty.
struct NoReports;
impl ReportsPort for NoReports {
    fn forget_reports(&mut self) {}
    fn announce(&mut self, _: &Announcement) -> Result<(), StepError> {
        panic!("the serving step never announces outside its fence")
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        Ok(BTreeMap::new())
    }
    fn members(&mut self) -> Vec<MemberRow> {
        Vec::new()
    }
    fn own_node_id(&mut self) -> Option<String> {
        None
    }
    fn member_identity(&mut self, _: &str) -> Value {
        Value::Null
    }
    fn clock_skew(&mut self, _: &[String]) -> Result<Option<ClockSkew>, StepError> {
        Ok(None)
    }
    fn tick_local(&mut self) {}
}

/// The run's own workflow as the serving step reads it: `starting`, and bound to the run until
/// the host's work stop, exactly as `OwnerWork::current` counts its own stop flag
/// (api joined_owner/work.rs). The first status read, which the wait makes after its Stop check
/// (`ctx.check_stopped()` then `up()`, demo_chain.py:1016-1022), parks until the test releases it.
pub(super) struct StoppableWorkflow {
    pub(super) stopped: Arc<AtomicBool>,
    pub(super) entered: Option<mpsc::Sender<()>>,
    pub(super) release: mpsc::Receiver<()>,
}
impl WorkPort for StoppableWorkflow {
    fn install_cleanup_progress(&mut self) -> Result<(), StepError> {
        panic!("publish only")
    }
    fn start_serving(&mut self, _: &Value) -> Result<Value, PortError> {
        panic!("publish only")
    }
    fn workflow_state(&mut self) -> Result<Value, StepError> {
        if let Some(entered) = self.entered.take()
            && entered.send(()).is_ok()
        {
            hold(&self.release);
        }
        Ok(json!({"state": "starting", "error_code": null}))
    }
    fn serving_identity(&mut self, _: &RunState) -> Result<ServingIdentity, StepError> {
        Ok(if self.stopped.load(Ordering::SeqCst) {
            ServingIdentity::Changed
        } else {
            ServingIdentity::Current
        })
    }
}

/// Every step but `serving` is answered `Ok`; `serving` is the REAL step over the workflow above.
pub(super) struct RealServingStep {
    pub(super) workflow: StoppableWorkflow,
}
impl Chain for RealServingStep {
    fn new_run(
        &mut self,
        _: &str,
        _: &str,
        _: owner_run::Reason,
        _: Option<&Value>,
        _: Option<&Value>,
    ) {
    }
    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        if step != "serving" {
            return Ok(());
        }
        let mut state = RunState::new("quit-run", "quit-deployment", owner_run::Reason::Start);
        let poll = span(BudgetId::OwnerPoll).unwrap().as_secs_f64();
        run_serving_step(env, &mut NoReports, &mut self.workflow, &mut state, poll)
    }
    fn teardown(&mut self, _: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        Ok(())
    }
}

/// Proves: B-RUN-10 (REVIEW-laneSRV-r2-astra R2A-02 = r2-opus R2-3: v0.1.4's Quit runs
/// `run.stop(); run.join(60)` (demo_app.py:199-203) before `workflow.stop()` (app.py:1835-1838),
/// so a serving step already past its Stop check never sees a workflow Stop it did not cause: the
/// REAL serving predicate, parked after its Stop check while Quit begins, ends the run `stopped`
/// with no error, and the workflow is stopped only after the run ended.)
#[test]
fn quit_during_the_serving_wait_ends_the_run_stopped_not_failed() {
    let f = Fixture::new();
    let stopped = Arc::new(AtomicBool::new(false));
    let (entered_tx, entered) = mpsc::channel();
    let (release, release_rx) = mpsc::channel::<()>();
    let mut chain: Option<Box<dyn Chain>> = Some(Box::new(RealServingStep {
        workflow: StoppableWorkflow {
            stopped: stopped.clone(),
            entered: Some(entered_tx),
            release: release_rx,
        },
    }));
    let config = RunConfig::default();
    let run = OwnerRun::install_with(
        f.host.clone(),
        &config,
        None,
        Box::new(move || chain.take().expect("one run")),
    );
    // Dropped before the run when an assertion unwinds: the parked read is released first.
    let release = release;
    lock(&run.runs)
        .demo_action(Action::DemoStart, &Map::new(), &mut Facts)
        .expect("start");
    entered
        .recv_timeout(patience())
        .expect("the serving predicate reads the workflow after its Stop check");
    let view = config.observed().unwrap();
    let (seen_tx, seen) = mpsc::channel();
    let seen_tx = Mutex::new(seen_tx);
    let flag = stopped.clone();
    lock(&f.host.custody).work_stop = Some(Arc::new(move || {
        flag.store(true, Ordering::SeqCst);
        let state = view.status().map(|s| s.to_value()["state"].clone());
        assert!(lock(&seen_tx).send(state).is_ok(), "test listens");
    }));
    let mut close = RunClose::new(config.clone());
    close.begin();
    // The predicate resumes only now, with Quit begun.
    release.send(()).expect("the predicate waits");
    let at_work_stop = seen.recv_timeout(patience());
    let until = Instant::now() + patience();
    let closed = loop {
        if let Some(result) = close.poll() {
            break Some(result);
        }
        if Instant::now() >= until {
            break None;
        }
        std::thread::yield_now();
    };
    let last = config.observed().unwrap().status().unwrap().to_value();
    lock(&f.host.custody).work_stop = None;
    assert_eq!(
        (&last["state"], &last["error_code"]),
        (&json!("stopped"), &Value::Null),
        "Quit must end a serving run stopped, not failed: {last}"
    );
    assert_eq!(
        at_work_stop.expect("Quit stops the work"),
        Some(json!("stopped")),
        "the workflow is stopped only once the run has ended"
    );
    assert!(matches!(closed, Some(Ok(()))), "Quit ended the run");
}

/// A run whose `slices` step blocks in a work call that only the workflow's own Stop releases
/// (as a slices push waits on `OwnerWork`'s stop flag, not on the run's stop event).
struct BlockedOnWork {
    entered: mpsc::Sender<()>,
    work_released: mpsc::Receiver<()>,
}
impl Chain for BlockedOnWork {
    fn new_run(
        &mut self,
        _: &str,
        _: &str,
        _: owner_run::Reason,
        _: Option<&Value>,
        _: Option<&Value>,
    ) {
    }
    fn run_step(&mut self, step: &str, _: &mut dyn StepEnv) -> Result<(), StepError> {
        if step != "slices" {
            return Ok(());
        }
        assert!(self.entered.send(()).is_ok(), "test listens");
        let released = self.work_released.recv_timeout(patience());
        assert!(released.is_ok(), "only the work stop releases this call");
        Err(StepError::Stopped)
    }
    fn teardown(&mut self, _: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        Ok(())
    }
}

/// Proves: B-RUN-10 (REVIEW-laneSRV-r2-astra R2A-02, the interrupt fallback: outside the serving
/// step Quit still stops the workflow as it begins, after the run's Stop, so a work call that
/// waits on the workflow's own stop flag is released at once instead of holding Quit for the
/// whole `run.join(60)` allowance (demo_app.py:203); the run then ends `stopped`.)
#[test]
fn quit_outside_the_serving_step_releases_a_blocked_work_call_at_once() {
    let f = Fixture::new();
    let (entered_tx, entered) = mpsc::channel();
    let (work_tx, work_released) = mpsc::channel();
    let mut chain: Option<Box<dyn Chain>> = Some(Box::new(BlockedOnWork {
        entered: entered_tx,
        work_released,
    }));
    let config = RunConfig::default();
    let run = OwnerRun::install_with(
        f.host.clone(),
        &config,
        None,
        Box::new(move || chain.take().expect("one run")),
    );
    lock(&run.runs)
        .demo_action(Action::DemoStart, &Map::new(), &mut Facts)
        .expect("start");
    entered
        .recv_timeout(patience())
        .expect("the slices step blocks in its work call");
    let view = config.observed().unwrap();
    let (seen_tx, seen) = mpsc::channel();
    let stopper = Mutex::new((seen_tx, work_tx));
    lock(&f.host.custody).work_stop = Some(Arc::new(move || {
        let state = view.status().map(|s| s.to_value()["state"].clone());
        let stopper = lock(&stopper);
        assert!(stopper.0.send(state).is_ok(), "test listens");
        assert!(stopper.1.send(()).is_ok(), "the blocked work call listens");
    }));
    let mut close = RunClose::new(config.clone());
    close.begin();
    // Synchronously within `begin`: the run's Stop first, then the work stop.
    let at_begin = seen.try_recv();
    let until = Instant::now() + patience();
    let closed = loop {
        if let Some(result) = close.poll() {
            break Some(result);
        }
        if Instant::now() >= until {
            break None;
        }
        std::thread::yield_now();
    };
    let last = config.observed().unwrap().status().unwrap().to_value();
    lock(&f.host.custody).work_stop = None;
    assert_eq!(
        at_begin,
        Ok(Some(json!("stopping"))),
        "Quit stops the workflow as it begins, after the run's Stop"
    );
    assert!(matches!(closed, Some(Ok(()))), "Quit ended the run");
    assert_eq!(last["state"], "stopped", "{last}");
}

/// A run that serves at once and whose monitor parks on its first call: the host's status loop
/// is then inside a live status pass that waits for it.
struct ParkedMonitor {
    entered: Option<mpsc::Sender<()>>,
    release: mpsc::Receiver<()>,
}
impl Chain for ParkedMonitor {
    fn new_run(
        &mut self,
        _: &str,
        _: &str,
        _: owner_run::Reason,
        _: Option<&Value>,
        _: Option<&Value>,
    ) {
    }
    fn run_step(&mut self, _: &str, _: &mut dyn StepEnv) -> Result<(), StepError> {
        Ok(())
    }
    fn teardown(&mut self, _: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        Ok(())
    }
    fn rollover_due(&mut self) -> Option<bool> {
        Some(false)
    }
    fn ceiling_reached(&mut self) -> owner_run::machine::CeilingProbe {
        owner_run::machine::CeilingProbe::Code(None)
    }
    fn monitor(&mut self) -> owner_run::machine::MonitorProbe {
        if let Some(entered) = self.entered.take()
            && entered.send(()).is_ok()
        {
            hold(&self.release);
        }
        owner_run::machine::MonitorProbe::Code(None)
    }
}

/// Proves: B-RUN-10 (REVIEW-laneSRV-r2-opus R2-11: dropping the packaged host's status loop waits
/// for its thread at most `PublishedStatus.stop`'s `self.thread.join(2)` (published_status.py:78-81,
/// `PublishedstatusStopSelfThreadJoinL81`), then goes on, even while a live status pass waits on a
/// monitor call that has not returned.)
#[test]
fn dropping_the_status_loop_waits_for_it_at_most_the_source_join() {
    let f = Fixture::new();
    let (entered_tx, entered) = mpsc::channel();
    let (release, release_rx) = mpsc::channel::<()>();
    let mut chain: Option<Box<dyn Chain>> = Some(Box::new(ParkedMonitor {
        entered: Some(entered_tx),
        release: release_rx,
    }));
    let config = RunConfig::default();
    let run = OwnerRun::install_with(
        f.host.clone(),
        &config,
        None,
        Box::new(move || chain.take().expect("one run")),
    );
    let release = release;
    lock(&run.runs)
        .demo_action(Action::DemoStart, &Map::new(), &mut Facts)
        .expect("start");
    let period =
        span(BudgetId::PyMyceliumA13MacosPublishedStatusPublishedstatusLoopSelfStopWaitL73)
            .unwrap();
    entered
        .recv_timeout(period * 10)
        .expect("the status loop's live pass runs the monitor");
    let installed = lock(&config.installed.0).take();
    let (dropped_tx, dropped) = mpsc::channel();
    let dropping = std::thread::spawn(move || {
        drop(installed);
        assert!(dropped_tx.send(()).is_ok(), "test listens");
    });
    let join =
        span(BudgetId::PyMyceliumA13MacosPublishedStatusPublishedstatusStopSelfThreadJoinL81)
            .unwrap();
    let bounded = dropped.recv_timeout(join + span(BudgetId::RouterClose).unwrap());
    drop(release);
    dropping.join().expect("dropping thread");
    lock(&run.runs).close();
    assert!(
        bounded.is_ok(),
        "dropping the status loop must not wait past its join allowance"
    );
}

/// Proves: B-RUN-10 (REVIEW-laneSRV-r2-opus R2-10: with no workflow resident, the packaged
/// monitor keeps this Mac's own contributor reporting before it answers
/// `managed_workflow_missing`, as v0.1.4's `monitor` runs `self._tick_local()` between the ceiling
/// and the workflow read (demo_chain.py:1166-1176).)
#[test]
fn with_no_workflow_the_monitor_ticks_the_local_contributor_before_answering_missing() {
    use owner_run::{
        machine::MonitorProbe, owner_chain::ChainPorts as _,
        steps::contributor::ContributorPort as _,
    };
    let mut f = Fixture::new();
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let state = super::serving_r2::run_state(&mut f, &ports.work, "run-tick", "dep-tick");
    let mut env = Env { at: Instant::now() };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    ports.close_work().unwrap();
    let local = state.local_node.clone().unwrap();
    ports.contributor.attach_agent(&local).unwrap();
    let before = ports.reports.reports().unwrap();
    let probe = ports.monitor(&state);
    let after = ports.reports.reports().unwrap();
    assert_eq!(
        probe,
        MonitorProbe::Code(Some("managed_workflow_missing".into()))
    );
    assert!(
        !before.contains_key(&local),
        "this Mac had not reported yet: {before:?}"
    );
    assert!(
        after.contains_key(&local),
        "the monitor ticks this Mac's own contributor: {after:?}"
    );
}

/// The executed v0.1.4 publish/targets fixture (owner-run's `oracle_work_r15_v2.json`).
fn work_fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../../../owner-run/tests/fixtures/oracle_work_r15_v2.json"
    ))
    .unwrap()
}

/// The fixture's own time, clock reads and step outputs (as owner-run's `oracle_work.rs` replay).
#[derive(Default)]
struct Record {
    time: f64,
    calls: Vec<Value>,
    clocks: Vec<Value>,
    result: Value,
    progress: Vec<Value>,
    publishes: Vec<Value>,
    stop: Option<f64>,
}
struct Replayed<'a>(&'a Mutex<Record>);
impl StepEnv for Replayed<'_> {
    fn progress(&mut self, detail: &str) {
        lock(self.0).progress.push(json!(detail));
    }
    fn stopped(&self) -> bool {
        let rec = lock(self.0);
        rec.stop.is_some_and(|at| rec.time >= at)
    }
    fn now(&mut self) -> f64 {
        let mut rec = lock(self.0);
        let t = rec.time;
        rec.clocks.push(json!(["mono", t]));
        t
    }
    fn wall(&mut self) -> f64 {
        let mut rec = lock(self.0);
        let t = 1_800_000_000.0 + rec.time;
        rec.clocks.push(json!(["wall", t]));
        t
    }
    fn set_result(&mut self, key: &str, value: Value) {
        let mut rec = lock(self.0);
        if key == "participants" {
            rec.publishes.push(value.clone());
        }
        rec.result[key] = value;
    }
    fn record_exception(&mut self, _: &str, _: &str) {
        panic!("unexpected cleanup exception");
    }
    fn pause(&mut self, seconds: f64) -> bool {
        lock(self.0).time += seconds;
        self.stopped()
    }
}
/// The fixture's exchange double: its report frames, members and the local tick.
struct FixtureReports<'a>(&'a Mutex<Record>, Value);
impl ReportsPort for FixtureReports<'_> {
    fn forget_reports(&mut self) {
        panic!("cannot forget");
    }
    fn announce(&mut self, _: &Announcement) -> Result<(), StepError> {
        panic!("cannot announce");
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        let mut rec = lock(self.0);
        rec.calls.push(json!("reports"));
        let frame = self.1["reports"]
            .as_array()
            .unwrap()
            .iter()
            .rfind(|f| f[0].as_f64().unwrap() <= rec.time)
            .map_or(Value::Null, |f| f[1].clone());
        Ok(serde_json::from_value(frame).unwrap())
    }
    fn members(&mut self) -> Vec<MemberRow> {
        lock(self.0).calls.push(json!("members"));
        self.1["members"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| MemberRow {
                node_id: m["node_id"].as_str().unwrap().into(),
                lifecycle_state: m["lifecycle_state"].as_str().map(str::to_owned),
                lease_expires_at: m["lease_expires_at"].as_f64(),
            })
            .collect()
    }
    fn own_node_id(&mut self) -> Option<String> {
        Some("owner-authority".into())
    }
    fn member_identity(&mut self, _: &str) -> Value {
        panic!("no identity reread");
    }
    fn clock_skew(&mut self, _: &[String]) -> Result<Option<ClockSkew>, StepError> {
        panic!("no skew read");
    }
    fn tick_local(&mut self) {
        lock(self.0).calls.push(json!("tick"));
    }
}
/// The fixture case's run state (owner-run `oracle_work.rs` `state_of`).
fn work_state(spec: &Value) -> RunState {
    use owner_run::steps::model::{ModelStepOutput, PreparedModel, PreparedSource};
    let s = &spec["state"];
    let m = &spec["model"];
    let mut state = RunState::new("run-1", "dep-1", owner_run::Reason::Start);
    state.local_node = s["local_node"].as_str().map(str::to_owned);
    state.descriptors = serde_json::from_value(s["descriptors"].clone()).unwrap();
    state.rollover_binding = s.get("rollover_binding").cloned();
    state.participants = serde_json::from_value(s["participants"].clone()).unwrap();
    state.proposal = s.get("proposal").cloned();
    state.compile_state = (!s["compile_state"].is_null()).then(|| s["compile_state"].clone());
    state.worker_wall_limits = serde_json::from_value(s["worker_wall_limits"].clone()).unwrap();
    state.layers = serde_json::from_value(s["layers"].clone()).unwrap();
    let digest: String = s["manifest_digest"].as_str().unwrap().into();
    state.model = Some(ModelStepOutput {
        forced_slice: false,
        prepared: PreparedSource {
            model: PreparedModel {
                model_id: m["model_id"].as_str().unwrap().into(),
                resolved_commit: m["resolved_commit"].as_str().unwrap().into(),
                architecture: m["architecture"].as_str().unwrap().into(),
                label: m["label"].as_str().unwrap().into(),
                choice: m["choice"].as_str().map(str::to_owned),
                shape_registered: true,
                owner_storage_need: m["owner_storage_need"].as_u64().unwrap(),
            },
            manifest_digest: digest.clone(),
            source_bytes: json!(123),
        },
        choice: m["choice"].as_str().map(str::to_owned),
        slice_mode: s["slice_mode"].as_bool().unwrap(),
        manifest_digest: digest,
        model_id: m["model_id"].as_str().unwrap().into(),
        resolved_commit: m["resolved_commit"].as_str().unwrap().into(),
    });
    state.execution_clock_started_at = s["execution_clock_started_at"].as_f64();
    state.registry_origin = s.get("registry_origin").cloned();
    state
}
fn rle(xs: &[Value]) -> Value {
    let mut out: Vec<Value> = vec![];
    for x in xs {
        if let Some(last) = out.last_mut().filter(|v| v[0] == *x) {
            last[1] = json!(last[1].as_u64().unwrap() + 1);
        } else {
            out.push(json!([x, 1]));
        }
    }
    json!(out)
}

/// Proves: B-RUN-10 (REVIEW-laneSRV-r2-opus R2-9: the packaged host's absent-workflow `null`
/// (round 2) reaches the targets step's `_workflow_state() or {}` (demo_chain.py:434-436, 952):
/// the REAL targets step over the REAL packaged adapter with no workflow resident ends exactly as
/// the executed v0.1.4 case `targets-no-workflow` does: outcome, results, published
/// participants, collaborator calls, clock reads and elapsed time.)
#[test]
fn the_targets_step_over_an_absent_packaged_workflow_matches_its_oracle_case() {
    let f = Fixture::new();
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    assert!(
        lock(&ports.work.resident).work.is_none(),
        "no workflow is resident"
    );
    let fixture = work_fixture();
    let case = &fixture["cases"]["targets-no-workflow"];
    let spec = &case["spec"];
    assert_eq!(spec["no_workflow"], true);
    let rec = Mutex::new(Record {
        result: json!({}),
        stop: spec["stop_at"].as_f64(),
        ..Record::default()
    });
    let mut state = work_state(spec);
    let poll = span(BudgetId::OwnerPoll).unwrap().as_secs_f64();
    let result = owner_run::steps::targets::run_targets_step(
        &mut Replayed(&rec),
        &mut FixtureReports(&rec, spec.clone()),
        &mut ports.work,
        &mut state,
        poll,
    );
    let rec = lock(&rec);
    let outcome = match result {
        Ok(()) => json!({"kind": "ok"}),
        Err(StepError::Stopped) => json!({"kind": "stopped"}),
        Err(StepError::Exception { class }) => json!({"kind": "exception", "class": class}),
        Err(StepError::Refused(e)) => {
            json!({"kind": "refused", "code": e.code, "message": e.message})
        }
    };
    assert_eq!(outcome, case["outcome"]);
    assert_eq!(rec.result, case["result"]);
    assert_eq!(rle(&rec.progress), case["progress"]);
    assert_eq!(rle(&rec.publishes), case["publishes"]);
    assert_eq!(rle(&rec.calls), case["calls"]);
    assert_eq!(rle(&rec.clocks), case["clocks"]);
    assert_eq!(json!(rec.time), case["time"]);
}

/// Proves: B-RUN-10 (REVIEW-laneSRV-r2-opus R2-9: the absent-workflow `null` cannot reach the
/// publish step's workflow read on the packaged host: with no workflow resident, publish ends
/// where it ended before round 2, refusing `owner_work_unavailable` at its first call
/// (`install_cleanup_progress`), before any clock read, workflow read or announcement. v0.1.4's
/// `publish-no-workflow` case instead skips the install and starts serving through
/// `ChainOps.start_serving`, which composes no workflow on this host: a pre-existing difference
/// of the packaged composition (its workflow is composed in the slices step), not of the
/// absent-workflow read, and outside lane SRV.)
#[test]
fn the_publish_step_over_an_absent_packaged_workflow_refuses_before_reading_it() {
    let f = Fixture::new();
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let fixture = work_fixture();
    let spec = &fixture["cases"]["publish-no-workflow"]["spec"];
    let rec = Mutex::new(Record {
        result: json!({}),
        ..Record::default()
    });
    let mut state = work_state(spec);
    let poll = span(BudgetId::OwnerPoll).unwrap().as_secs_f64();
    let result = owner_run::steps::publish::run_publish_step(
        &mut Replayed(&rec),
        &mut FixtureReports(&rec, spec.clone()),
        &mut ports.work,
        &mut state,
        poll,
    );
    let rec = lock(&rec);
    assert!(
        matches!(&result, Err(StepError::Refused(e)) if e.code == "owner_work_unavailable"),
        "{result:?}"
    );
    assert_eq!(rec.result, json!({}), "nothing published");
    assert!(
        rec.calls.is_empty() && rec.clocks.is_empty(),
        "{:?} {:?}",
        rec.calls,
        rec.clocks
    );
    assert_eq!(state.execution_clock_started_at, None);
}
