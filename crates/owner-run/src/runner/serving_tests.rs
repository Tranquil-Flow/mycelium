// SPDX-License-Identifier: AGPL-3.0-only
//! The REAL serving step ([`run_serving_step`]) driven by the threaded Runner, with its
//! publication parked inside the owner's fence (REVIEW-laneSRV-r2-opus R2-1,
//! REVIEW-laneSRV-r2-astra R2A-04). v0.1.4 commits the announcement and writes
//! `ctx.result['registry_origin']` inside one `with owner._lock` (demo_chain.py:1075-1097), the lock
//! `status_snapshot()` and `OwnerRun.stop` take (owner_run.py:194, 275-282): nothing can observe
//! the announcement without its origin. Deterministic: the commit parks on a channel, and the
//! test releases it only once a status reader and a Stop are each PROVEN blocked on the run lock
//! (they found it held), never by timing.
use super::test_hooks::Blocked;
use super::*;
use crate::state::RunState;
use crate::steps::exchange::{Announcement, ClockSkew, MemberRow, ReportsPort};
use crate::steps::model::{ModelStepOutput, PreparedModel, PreparedSource};
use crate::steps::publish::{Fence, ServingIdentity, WorkPort};
use crate::steps::serving::run_serving_step;
use serde_json::json;
use std::collections::BTreeMap;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    mpsc,
};

const ORIGIN: &str = "http://127.0.0.1:4385";

fn patience() -> Duration {
    Duration::from_secs_f64(Timing::registered().rebuild_join)
}

/// Waits for a release; a dropped sender (the test unwound) releases as well.
fn hold(release: &mpsc::Receiver<()>) {
    let timed_out = matches!(
        release.recv_timeout(patience()),
        Err(mpsc::RecvTimeoutError::Timeout)
    );
    assert!(!timed_out, "test release timed out");
}

/// No members and no reports: the step's report reads are empty.
struct NoReports;
impl ReportsPort for NoReports {
    fn forget_reports(&mut self) {}
    fn announce(&mut self, _: &Announcement) -> Result<(), StepError> {
        panic!("the serving announcement goes through the workflow fence")
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

/// A qualified, `api_running` workflow whose in-memory commit announces, then parks.
struct ParkedCommit {
    announced: Arc<AtomicBool>,
    entered: mpsc::Sender<()>,
    release: mpsc::Receiver<()>,
}
impl WorkPort for ParkedCommit {
    fn install_cleanup_progress(&mut self) -> Result<(), StepError> {
        panic!("publish only")
    }
    fn start_serving(&mut self, _: &Value) -> Result<Value, crate::chain::PortError> {
        panic!("publish only")
    }
    fn workflow_state(&mut self) -> Result<Value, StepError> {
        Ok(
            json!({"state": "api_running", "error_code": null, "startup": {
            "startup_qualification_complete": true, "unavailable_deployment_count": 0,
            "url": format!("{ORIGIN}/")}}),
        )
    }
    fn serving_identity(&mut self, _: &RunState) -> Result<ServingIdentity, StepError> {
        Ok(ServingIdentity::NotNeeded)
    }
    fn commit_serving(
        &mut self,
        _: &RunState,
        _: &Announcement,
        _: Option<&Value>,
        fence: Fence<'_>,
    ) -> Result<(), StepError> {
        let (announced, entered, release) = (&self.announced, &self.entered, &self.release);
        fence(&mut || {
            announced.store(true, Ordering::SeqCst);
            // A failed send means the test already unwound: do not park.
            if entered.send(()).is_ok() {
                hold(release);
            }
            Ok(())
        })
    }
}

/// The Runner's environment, unchanged, except that a `registry_origin` result written
/// OUTSIDE the fence (through `set_result`, not `fenced`) parks before it reaches the run: the
/// gap such a write leaves after the fence becomes a fact the test observes, not a race.
struct OutsideWrites<'a> {
    inner: &'a mut dyn StepEnv,
    outside: mpsc::Sender<()>,
    release: &'a mpsc::Receiver<()>,
}
impl StepEnv for OutsideWrites<'_> {
    fn progress(&mut self, detail: &str) {
        self.inner.progress(detail);
    }
    fn stopped(&self) -> bool {
        self.inner.stopped()
    }
    fn now(&mut self) -> f64 {
        self.inner.now()
    }
    fn wall(&mut self) -> f64 {
        self.inner.wall()
    }
    fn set_result(&mut self, key: &str, value: Value) {
        if key == "registry_origin" && self.outside.send(()).is_ok() {
            hold(self.release);
        }
        self.inner.set_result(key, value);
    }
    fn record_exception(&mut self, code: &str, class: &str) {
        self.inner.record_exception(code, class);
    }
    fn pause(&mut self, seconds: f64) -> bool {
        self.inner.pause(seconds)
    }
    fn fenced(
        &mut self,
        commit: &mut dyn FnMut() -> Result<(), StepError>,
        key: &str,
        value: Value,
    ) -> Result<(), StepError> {
        self.inner.fenced(commit, key, value)
    }
}

/// Every step but `serving` is answered `Ok`; `serving` is the real step over the doubles above.
struct ServingChain {
    workflow: ParkedCommit,
    outside: mpsc::Sender<()>,
    outside_release: mpsc::Receiver<()>,
    outcome: mpsc::Sender<Result<(), StepError>>,
}
impl Chain for ServingChain {
    fn new_run(&mut self, _: &str, _: &str, _: Reason, _: Option<&Value>, _: Option<&Value>) {}
    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        if step != "serving" {
            return Ok(());
        }
        let mut state = RunState::new("origin-run", "origin-deployment", Reason::Start);
        let prepared = PreparedSource {
            model: PreparedModel {
                model_id: "Qwen/Qwen3-0.6B".into(),
                resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
                architecture: "qwen3".into(),
                label: "Qwen3 0.6B".into(),
                choice: None,
                shape_registered: true,
                owner_storage_need: 1,
            },
            manifest_digest: "sha256:00".into(),
            source_bytes: json!(1),
        };
        state.model = Some(ModelStepOutput {
            forced_slice: false,
            prepared,
            choice: None,
            slice_mode: true,
            manifest_digest: "sha256:00".into(),
            model_id: "Qwen/Qwen3-0.6B".into(),
            resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
        });
        let mut env = OutsideWrites {
            inner: env,
            outside: self.outside.clone(),
            release: &self.outside_release,
        };
        let result = run_serving_step(
            &mut env,
            &mut NoReports,
            &mut self.workflow,
            &mut state,
            Timing::registered().poll.as_secs_f64(),
        );
        assert!(self.outcome.send(result.clone()).is_ok(), "test listens");
        result
    }
    fn teardown(&mut self, _: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        Ok(())
    }
}

/// Proves: B-RUN-10 (REVIEW-laneSRV-r2-opus R2-1 / r2-astra R2A-04: the REAL serving step writes
/// `registry_origin` inside the Runner's one transaction with its announcement. While the
/// publication is parked inside the fence, a status read and a Stop from other threads are both
/// proven blocked on the run lock; once released, neither observes the announcement without
/// its origin (demo_chain.py:1075-1097, owner_run.py:194, 275-282).)
#[test]
fn the_real_serving_step_publishes_its_origin_inside_the_transaction() {
    let announced = Arc::new(AtomicBool::new(false));
    let (entered_tx, entered) = mpsc::channel();
    let (release, release_rx) = mpsc::channel::<()>();
    let (outside_tx, outside) = mpsc::channel();
    let (outside_release, outside_release_rx) = mpsc::channel::<()>();
    let (outcome_tx, outcome) = mpsc::channel();
    let runner = Runner::spawn(
        Box::new(ServingChain {
            workflow: ParkedCommit {
                announced: announced.clone(),
                entered: entered_tx,
                release: release_rx,
            },
            outside: outside_tx,
            outside_release: outside_release_rx,
            outcome: outcome_tx,
        }),
        Box::new(|| Identity {
            run_id: "origin-run".into(),
            deployment_id: "origin-deployment".into(),
        }),
        Arc::new(SystemClocks::new()),
        Timing::registered(),
    )
    .expect("runner");
    // Dropped before the Runner when an assertion unwinds: every park is released first.
    let release = release;
    let outside_release = outside_release;
    let blocked = Blocked::watch(&runner.shared.run);

    runner.start("start", None).expect("start");
    entered
        .recv_timeout(patience())
        .expect("the commit is parked inside the fence");
    let view = runner.view();
    let (reader_id, reader_ids) = mpsc::channel();
    let (stopper_id, stopper_ids) = mpsc::channel();
    let ((announced_before_read, snapshot), answer) = std::thread::scope(|scope| {
        let flag = announced.clone();
        let reader = scope.spawn(move || {
            assert!(reader_id.send(std::thread::current().id()).is_ok());
            (flag.load(Ordering::SeqCst), view.snapshot())
        });
        let stopper = scope.spawn(|| {
            assert!(stopper_id.send(std::thread::current().id()).is_ok());
            runner.stop()
        });
        let reader_id = reader_ids.recv_timeout(patience()).expect("reader started");
        let stopper_id = stopper_ids
            .recv_timeout(patience())
            .expect("stopper started");
        assert!(
            blocked.wait_for(reader_id, patience()),
            "the status read must wait behind the parked publication"
        );
        assert!(
            blocked.wait_for(stopper_id, patience()),
            "the Stop must wait behind the parked publication"
        );
        release.send(()).expect("the commit waits for its release");
        (
            reader.join().expect("reader"),
            stopper.join().expect("stopper"),
        )
    });
    let wrote_outside = outside.try_recv().is_ok();
    drop(outside_release);
    let published = outcome.recv_timeout(patience()).expect("the step ended");
    runner.close();
    assert!(
        announced_before_read,
        "the read started after the announcement"
    );
    assert_eq!(
        snapshot["registry_origin"],
        json!(ORIGIN),
        "a status read never shows the announcement without its origin: {snapshot}"
    );
    assert_eq!(answer["state"], "stopping", "{answer}");
    assert_eq!(
        answer["registry_origin"],
        json!(ORIGIN),
        "a Stop never lands between the announcement and its origin: {answer}"
    );
    assert_eq!(published, Ok(()), "the parked publication completes");
    assert!(
        !wrote_outside,
        "the step wrote registry_origin outside the fence"
    );
}
