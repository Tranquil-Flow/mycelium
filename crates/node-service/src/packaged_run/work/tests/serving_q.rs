// SPDX-License-Identifier: AGPL-3.0-only
//! Lane Q part B (GOAL ledger row 42): the HTTP Stop (`/api/demo/stop`) during the serving step
//! has Quit's rule (`RunClose::begin_until`, 1746563f). v0.1.4's Stop only stops the run
//! (`OwnerRun.stop`, owner_run.py:275-283); the run's own teardown stops the workflow. Model-free:
//! no native, no route.
use super::serving_r3::{Facts, RealServingStep, StoppableWorkflow, hold, patience};
use super::*;
use api::actions::Action;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;

/// The real serving step (as `serving_r3`) whose run teardown stops the workflow, as the packaged
/// chain's teardown does (`PackagedChainPorts::teardown` -> `close_work`).
struct ServingThenTeardown {
    step: RealServingStep,
    host: PackagedRun,
}
impl Chain for ServingThenTeardown {
    fn new_run(
        &mut self,
        run: &str,
        deployment: &str,
        reason: owner_run::Reason,
        binding: Option<&Value>,
        rollover: Option<&Value>,
    ) {
        self.step
            .new_run(run, deployment, reason, binding, rollover);
    }
    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        self.step.run_step(step, env)
    }
    fn teardown(&mut self, _: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        self.host.stop_work();
        Ok(())
    }
}

/// The run's status once it ended `stopped` or `failed` (bounded by `run.join(60)`'s allowance).
fn ended(config: &RunConfig) -> Value {
    let view = config.observed().unwrap();
    let until = Instant::now() + patience();
    loop {
        let status = view.status().unwrap().to_value();
        let done = matches!(status["state"].as_str(), Some("stopped" | "failed"));
        if done || Instant::now() >= until {
            return status;
        }
        std::thread::yield_now();
    }
}

/// Proves: B-RUN-10 (lane Q part B, ledger row 42: v0.1.4's `/api/demo/stop` only stops the run
/// (`demo_app.action` -> `OwnerRun.stop`, owner_run.py:275-283); the workflow is stopped by the
/// run's own teardown. The REAL serving predicate, parked after its Stop check while the HTTP
/// Stop is answered, ends the run `stopped` with no error, and the workflow is stopped only by the
/// run's teardown, never by the Stop handler while the step still reads it.)
#[test]
fn an_http_stop_during_the_serving_wait_ends_the_run_stopped_not_failed() {
    let f = Fixture::new();
    let stopped = Arc::new(AtomicBool::new(false));
    let (entered_tx, entered) = mpsc::channel();
    let (release, release_rx) = mpsc::channel::<()>();
    let mut chain: Option<Box<dyn Chain>> = Some(Box::new(ServingThenTeardown {
        step: RealServingStep {
            workflow: StoppableWorkflow {
                stopped: stopped.clone(),
                entered: Some(entered_tx),
                release: release_rx,
            },
        },
        host: f.host.clone(),
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
    let answer = run.demo_action(Action::DemoStop, &Map::new());
    let answered_step = answer.as_ref().map(|a| a.to_value()["step"].clone());
    let at_answer = seen.try_recv();
    // The predicate resumes only now, with the Stop answered.
    release.send(()).expect("the predicate waits");
    let last = ended(&config);
    let at_teardown = seen.recv_timeout(patience());
    lock(&f.host.custody).work_stop = None;
    assert_eq!(answered_step, Ok(json!("serving")), "{answer:?}");
    assert!(
        at_answer.is_err(),
        "the Stop handler must not stop the workflow during serving: {at_answer:?}; the run \
         ended {}: {}",
        last["state"],
        last["error_code"]
    );
    assert_eq!(
        (&last["state"], &last["error_code"]),
        (&json!("stopped"), &Value::Null),
        "an HTTP Stop must end a serving run stopped, not failed: {last}"
    );
    assert_eq!(
        at_teardown.expect("the run's teardown stops the work"),
        Some(json!("stopping")),
        "the workflow is stopped by the run's own teardown"
    );
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
        hold(&self.work_released);
        Err(StepError::Stopped)
    }
    fn teardown(&mut self, _: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        Ok(())
    }
}

/// Proves: B-RUN-10 (lane Q part B, Quit's interrupt fallback for the HTTP Stop: outside the
/// serving step a work call can wait on the workflow's own stop flag, which the run's Stop
/// cannot reach, so the Stop handler stops the workflow at once, after the run's Stop, and the
/// run ends `stopped` instead of holding the Stop for the step's whole budget.)
#[test]
fn an_http_stop_outside_the_serving_step_releases_a_blocked_work_call_at_once() {
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
    let answer = run.demo_action(Action::DemoStop, &Map::new());
    // Synchronously within the Stop handler: the run's Stop first, then the work stop.
    let at_answer = seen.try_recv();
    let last = ended(&config);
    lock(&f.host.custody).work_stop = None;
    assert!(answer.is_ok(), "{answer:?}");
    assert_eq!(
        at_answer,
        Ok(Some(json!("stopping"))),
        "outside serving the Stop handler stops the workflow at once, after the run's Stop"
    );
    assert_eq!(last["state"], "stopped", "{last}");
}
