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

/// `run.join(60)`'s allowance (demo_app.py:203): the longest any wait here may take.
fn patience() -> Duration {
    span(BudgetId::PyMyceliumA13MacosDemoAppCloseRunJoinL203).unwrap()
}

/// Waits for a release; a dropped sender (the test unwound) releases as well.
fn hold(release: &mpsc::Receiver<()>) {
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
struct StoppableWorkflow {
    stopped: Arc<AtomicBool>,
    entered: Option<mpsc::Sender<()>>,
    release: mpsc::Receiver<()>,
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
struct RealServingStep {
    workflow: StoppableWorkflow,
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
