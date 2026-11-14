// SPDX-License-Identifier: AGPL-3.0-only
//! A call of a replaced run context on the real Runner (REVIEW-laneSRV ASRV-02 / SRV-1). v0.1.4
//! gives every `RunContext` its own stop event (owner_run.py:96), writes progress only for the
//! current context (`ctx is self._ctx`, owner_run.py:334-337), keeps `ctx.result` per context and
//! refuses the serving publication when `owner._ctx is not ctx or owner._state != 'running'`
//! (demo_chain.py:1086-1087). Schedule: a step is parked, an async rebuild's join expires (the
//! machine is forced `failed` while the old call still runs), a new Start is accepted, then the
//! old call resumes. Deterministic: channels and the rebuild watcher's own join, no sleeps.
use super::*;
use serde_json::json;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    mpsc,
};

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

struct Held {
    calls: u32,
    old_entered: mpsc::Sender<()>,
    old_release: mpsc::Receiver<()>,
    new_entered: mpsc::Sender<()>,
    new_release: mpsc::Receiver<()>,
    committed: Arc<AtomicBool>,
    outcome: mpsc::Sender<(bool, Result<(), StepError>)>,
    teardowns: mpsc::Sender<bool>,
}
impl Chain for Held {
    fn new_run(&mut self, _: &str, _: &str, _: Reason, _: Option<&Value>, _: Option<&Value>) {}
    fn run_step(&mut self, _step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        self.calls += 1;
        if self.calls > 1 {
            // The NEW run's first step: park so the test reads a stable new run.
            if self.new_entered.send(()).is_ok() {
                hold(&self.new_release);
            }
            return Err(OwnerRunError::new("test_new_run_ends_here").into());
        }
        if self.old_entered.send(()).is_ok() {
            hold(&self.old_release);
        }
        // The OLD context resumes after a new run replaced it.
        let stopped = env.stopped();
        env.progress("progress of the replaced run");
        env.set_result("participants", json!(["replaced-run-node"]));
        env.record_exception("replaced_run_code", "ReplacedRunError");
        let committed = self.committed.clone();
        let result = env.fenced(
            &mut || {
                committed.store(true, Ordering::SeqCst);
                Ok(())
            },
            "registry_origin",
            json!("http://127.0.0.1:1/"),
        );
        assert!(
            self.outcome.send((stopped, result.clone())).is_ok(),
            "test listens"
        );
        result
    }
    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        env.set_result("cleanup_unproven", json!({"code": "replaced_run_teardown"}));
        assert!(self.teardowns.send(failed).is_ok(), "test listens");
        Ok(())
    }
}

/// Dropped before the Runner (declared after it): unwinding releases every parked call first.
struct Release {
    old: Option<mpsc::Sender<()>>,
    new: Option<mpsc::Sender<()>>,
}

/// Proves: B-RUN-10 (a step of a replaced run can never pass the serving fence, and none of its
/// writes reach the run that replaced it: its stop event stays set, its progress, results and
/// diagnostics stay in its own context, its outcome never advances the new run, and the publication
/// refuses, demo_chain.py:1074-1088 @ f4e366e9; the old context is still torn down, as the old
/// thread's `finally` does, owner_run.py:440-446).
#[test]
fn a_call_of_a_replaced_run_cannot_publish_or_write_into_the_new_run() {
    let (old_entered_tx, old_entered) = mpsc::channel();
    let (old_release_tx, old_release) = mpsc::channel();
    let (new_entered_tx, new_entered) = mpsc::channel();
    let (new_release_tx, new_release) = mpsc::channel();
    let (outcome_tx, outcome) = mpsc::channel();
    let (teardowns_tx, teardowns) = mpsc::channel();
    let committed = Arc::new(AtomicBool::new(false));
    let runner = Runner::spawn(
        Box::new(Held {
            calls: 0,
            old_entered: old_entered_tx,
            old_release,
            new_entered: new_entered_tx,
            new_release,
            committed: committed.clone(),
            outcome: outcome_tx,
            teardowns: teardowns_tx,
        }),
        Box::new(|| Identity {
            run_id: "replaced-run".into(),
            deployment_id: "replaced-deployment".into(),
        }),
        Arc::new(SystemClocks::new()),
        Timing {
            poll: Timing::registered().poll,
            // The join allowance expires at once: the old call stays parked past it.
            rebuild_join: 0.0,
        },
    )
    .expect("runner");
    let mut release = Release {
        old: Some(old_release_tx),
        new: Some(new_release_tx),
    };
    runner.start("start", None).expect("start");
    old_entered
        .recv_timeout(patience())
        .expect("old step entered");
    runner.rebuild_async().expect("rebuild");
    let watchers = std::mem::take(&mut *lock(&runner.watchers));
    for watcher in watchers {
        watcher.join().expect("rebuild watcher");
    }
    let failed = runner.snapshot();
    assert_eq!(failed["state"], "failed", "{failed}");
    assert_eq!(failed["error_code"], "owner_run_stop_incomplete");
    runner.start("start", None).expect("a new run starts");
    drop(release.old.take());
    let (old_saw_stop, fenced) = outcome.recv_timeout(patience()).expect("old step resumed");
    new_entered
        .recv_timeout(patience())
        .expect("the new run's first step began");
    let fresh = runner.snapshot();
    let old_teardown = teardowns.try_recv();
    drop(release.new.take());
    runner.close();
    assert!(old_saw_stop, "the replaced run's stop event stays set");
    assert_eq!(
        fenced,
        Err(StepError::Stopped),
        "the replaced run cannot publish"
    );
    assert!(!committed.load(Ordering::SeqCst), "nothing was committed");
    assert_eq!(
        old_teardown,
        Ok(false),
        "the replaced context was torn down"
    );
    assert_eq!(fresh["state"], "running", "{fresh}");
    assert_eq!(fresh["steps"][0]["state"], "active", "{fresh}");
    assert_eq!(fresh["steps"][0]["detail"], Value::Null, "{fresh}");
    assert_eq!(fresh["steps"][1]["state"], "pending", "{fresh}");
    assert_eq!(fresh["registry_origin"], Value::Null, "{fresh}");
    assert_eq!(fresh["participants"], Value::Null, "{fresh}");
    assert_eq!(fresh["cleanup_errors"], json!([]), "{fresh}");
    assert_eq!(fresh["cleanup_unproven"], Value::Null, "{fresh}");
}
