// SPDX-License-Identifier: AGPL-3.0-only
//! Lane SRV round 2 on the real packaged adapter (REVIEW-laneSRV ASRV-03/04/06, SRV-3/4/6/7):
//! the packaged host's own maintenance runs the serving run's live `status()` (monitor), an absent
//! workflow answers as v0.1.4 does, the announcement is prepared before the resident lock, and
//! Quit stops the run before its workflow. Model-free: no native, no route.
use super::*;
use api::actions::Action;
use owner_run::{
    action::DemoFacts,
    machine::{CeilingProbe, EXECUTION_CEILING_CODE, MonitorProbe},
    owner_chain::ChainPorts as _,
};
use std::sync::mpsc;

struct Facts;
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

fn wall_seconds() -> u64 {
    BudgetId::MemberChosenWall
        .span(Entries::ZERO)
        .unwrap()
        .millis()
        / 1000
}

fn status_period() -> Duration {
    span(BudgetId::PyMyceliumA13MacosPublishedStatusPublishedstatusLoopSelfStopWaitL73).unwrap()
}

/// The run's own state over the fixture's two Macs, with the compile phase announced.
fn run_state(f: &mut Fixture, work: &Work, run: &str, deployment: &str) -> RunState {
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    seed.exchange
        .set_owner_control(Some(OwnerControl::new(work.owner.public())));
    seed.exchange
        .announce(Announce {
            phase: "compile".into(),
            run_id: Some(run.into()),
            deployment_id: Some(deployment.into()),
            ..Default::default()
        })
        .unwrap();
    f.agent.tick(&f.member);
    let local = f
        .host
        .with(|c| c.run.contributor.as_ref().unwrap().node.clone());
    let mut state = RunState::new(run, deployment, owner_run::Reason::Start);
    state.participants = vec![local.clone(), f.remote.clone()];
    state.local_node = Some(local);
    state.worker_wall_limits = state
        .participants
        .iter()
        .cloned()
        .map(|n| (n, wall_seconds()))
        .collect();
    state
}

/// Proves: B-RUN-10 (REVIEW-laneSRV SRV-4: with no workflow resident the serving probes still
/// answer from the run's state on the Runner's clock, as v0.1.4 computes `rollover_due` and
/// `ceiling_reached` from `ctx.state` alone (demo_chain.py:1132-1164) and its `monitor` answers
/// `managed_workflow_missing` when `_workflow_state()` is None (:1174-1176), after the ceiling.)
#[test]
fn the_packaged_probes_answer_from_the_run_state_when_the_workflow_is_gone() {
    let mut f = Fixture::new();
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let state = run_state(&mut f, &ports.work, "run-gone", "dep-gone");
    let mut env = Env { at: Instant::now() };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    ports.close_work().unwrap();
    assert!(
        lock(&ports.work.resident).work.is_none(),
        "the workflow is gone"
    );
    let mut serving = state.clone();
    serving.work_limits = Some(json!({ "max_wall_seconds": wall_seconds() }));
    serving.execution_clock_started_at = Some(-(wall_seconds() as f64) * 10.0);
    let ceiling = Some(EXECUTION_CEILING_CODE.to_owned());
    assert_eq!(ports.rollover_due(&serving), Some(true));
    assert_eq!(
        ports.ceiling_reached(&serving),
        CeilingProbe::Code(ceiling.clone())
    );
    assert_eq!(ports.monitor(&serving), MonitorProbe::Code(ceiling));
    assert_eq!(ports.rollover_due(&state), Some(false));
    assert_eq!(ports.ceiling_reached(&state), CeilingProbe::Code(None));
    assert_eq!(
        ports.monitor(&state),
        MonitorProbe::Code(Some("managed_workflow_missing".into()))
    );
}

/// Proves: B-RUN-10 (REVIEW-laneSRV ASRV-06: the announcement is prepared, with its exchange clock
/// read and its validation, before the resident lock is taken, as v0.1.4 prepares before
/// `app.guard` (demo_chain.py:1056-1074): a refused preparation returns while another holder keeps
/// the resident lock.)
#[test]
fn the_serving_announcement_is_prepared_before_the_resident_lock() {
    use owner_run::steps::exchange::Announcement;
    let mut f = Fixture::new();
    let ports = PackagedChainPorts::new(&f.host, None, None);
    let mut state = run_state(&mut f, &ports.work, "run-prepare", "dep-prepare");
    let mut env = Env { at: Instant::now() };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    state.compile_state = Some(json!({"epoch": 7}));
    let announcement = Announcement {
        phase: "serving".into(),
        run_id: state.run_id.clone(),
        deployment_id: state.deployment_id.clone(),
        model_id: "Qwen/Qwen3-0.6B".into(),
        resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
        slice_mode: true,
        manifest_digest: "sha256:00".into(),
        // Not a per-node field: `_check_node_extra` refuses it (run_exchange.py:104-105).
        per_node: Some(vec![(f.remote.clone(), json!({"not_a_node_field": 1}))]),
    };
    let (done, finished) = mpsc::channel();
    let mut work = ports.work.clone();
    let held = lock(&ports.work.resident);
    let result = std::thread::scope(|scope| {
        let committing = scope.spawn(move || {
            let result = work.commit_serving(&state, &announcement, None, &mut |commit| commit());
            assert!(done.send(result).is_ok(), "test listens");
        });
        let result = finished.recv_timeout(span(BudgetId::RouterClose).unwrap());
        drop(held);
        committing.join().expect("commit thread");
        result
    });
    match result {
        Ok(Err(StepError::Exception { class })) => assert_eq!(class, "ValueError"),
        other => panic!("the preparation must refuse without the resident lock: {other:?}"),
    }
    let mut ports = ports;
    ports.close_work().unwrap();
}

/// The chain the packaged Runner drives for a serving run: every step is answered `Ok` by the test
/// (no native route exists), while the serving probes are the REAL `Composed` chain's, over its
/// resident owner workflow.
struct ServingComposed {
    inner: Composed,
    serving: mpsc::Sender<()>,
    teardown: mpsc::Sender<()>,
}
impl Chain for ServingComposed {
    fn new_run(
        &mut self,
        run: &str,
        deployment: &str,
        reason: owner_run::Reason,
        ceiling: Option<&Value>,
        binding: Option<&Value>,
    ) {
        self.inner
            .new_run(run, deployment, reason, ceiling, binding);
    }
    fn run_step(&mut self, _: &str, _: &mut dyn StepEnv) -> Result<(), StepError> {
        Ok(())
    }
    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        let result = self.inner.teardown(failed, env);
        assert!(self.teardown.send(()).is_ok(), "test listens");
        result
    }
    fn rollover_binding(&self) -> Option<Value> {
        self.inner.rollover_binding()
    }
    /// The Runner asks only a serving run: each call tells the test the run serves.
    fn rollover_due(&mut self) -> Option<bool> {
        assert!(self.serving.send(()).is_ok(), "test listens");
        self.inner.rollover_due()
    }
    fn ceiling_reached(&mut self) -> owner_run::machine::CeilingProbe {
        self.inner.ceiling_reached()
    }
    fn monitor(&mut self) -> owner_run::machine::MonitorProbe {
        self.inner.monitor()
    }
}

/// Proves: B-RUN-10 (REVIEW-laneSRV ASRV-03 / SRV-6: the packaged host itself runs the serving
/// run's live `status()` from maintenance, outside its host and run locks, as v0.1.4's
/// `PublishedStatus.refresh` calls `run.status()` every second (published_status.py:32-48, 72-76;
/// app.py:630-632) while readers stay observational: a serving run whose workflow reports a stale
/// member fails with that code although nothing read the page's status. Through the chain the
/// Runner drives (`Composed`), not a port method.)
#[test]
fn the_packaged_host_monitors_a_serving_run_without_any_status_read() {
    let mut f = Fixture::new();
    let composed = Composed::new(&f.host, None, None);
    let state = run_state(&mut f, &composed.work, "run-monitored", "dep-monitored");
    let mut env = Env { at: Instant::now() };
    composed
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    let leases = api::joined_owner::epoch::seed_members(&seed.coordinator);
    {
        // Every member's seed lease ran out long ago on the seed's clock.
        let mut resident = lock(&composed.work.resident);
        let work = resident.work.take().unwrap();
        resident.work = Some(work.with_seed_view(Arc::new(move |node: &str| {
            leases(node).map(|(row, now)| (row, now + 1e9))
        })));
    }
    let (serving_tx, serving) = mpsc::channel();
    let (teardown_tx, teardown) = mpsc::channel();
    let mut chain: Option<Box<dyn Chain>> = Some(Box::new(ServingComposed {
        inner: composed,
        serving: serving_tx,
        teardown: teardown_tx,
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
    let patience = status_period() * 10;
    serving.recv_timeout(patience).expect("the run serves");
    let monitored = teardown.recv_timeout(patience);
    let snapshot = config.observed().unwrap().status().unwrap().to_value();
    lock(&run.runs).close();
    assert!(
        monitored.is_ok(),
        "the host's maintenance must monitor the serving run: {snapshot}"
    );
    assert_eq!(
        snapshot["error_code"], "joined_route_member_stale",
        "{snapshot}"
    );
    assert_eq!(snapshot["failed_step"], "serving", "{snapshot}");
}

/// A run whose serving step waits for the owner's Stop on the run's own stop event.
struct WaitsForStop {
    entered: mpsc::Sender<()>,
}
impl Chain for WaitsForStop {
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
        assert!(self.entered.send(()).is_ok(), "test listens");
        let poll = span(BudgetId::OwnerPoll).unwrap().as_secs_f64();
        loop {
            if env.pause(poll) {
                return Err(StepError::Stopped);
            }
        }
    }
    fn teardown(&mut self, _: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        Ok(())
    }
}

/// Proves: B-RUN-10 (REVIEW-laneSRV SRV-7: Quit stops the run before its workflow, as v0.1.4's
/// close runs `demo_app.close` (`run.stop(); run.join(60)`, demo_app.py:199-203) before
/// `workflow.stop()` (app.py:1815-1837): when the work stop is requested the run is already
/// `stopping` or `stopped`, so a serving step ends `stopped`, never on a workflow that changed
/// under it.)
#[test]
fn quit_stops_the_run_before_its_workflow() {
    let f = Fixture::new();
    let (entered_tx, entered) = mpsc::channel();
    let mut chain: Option<Box<dyn Chain>> = Some(Box::new(WaitsForStop {
        entered: entered_tx,
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
    let patience = span(BudgetId::RouterClose).unwrap();
    entered
        .recv_timeout(patience)
        .expect("the serving step waits");
    let view = config.observed().unwrap();
    let (seen_tx, seen) = mpsc::channel();
    let seen_tx = Mutex::new(seen_tx);
    lock(&f.host.custody).work_stop = Some(Arc::new(move || {
        let state = view.status().map(|s| s.to_value()["state"].clone());
        assert!(lock(&seen_tx).send(state).is_ok(), "test listens");
    }));
    let mut close = RunClose::new(config.clone());
    close.begin();
    let at_work_stop = seen.recv_timeout(patience);
    let until = Instant::now() + patience;
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
    // The run thread may already have ended its stopped step (`stopped`) or not yet
    // (`stopping`); it is never still `running` when the workflow is asked to stop.
    let at_work_stop = at_work_stop.expect("Quit stops the work");
    assert!(
        matches!(
            at_work_stop.as_ref().and_then(Value::as_str),
            Some("stopping" | "stopped")
        ),
        "the run is stopped before its workflow: {at_work_stop:?}"
    );
    assert!(closed.is_some(), "Quit ended the run");
    assert_eq!(last["state"], "stopped", "{last}");
}
