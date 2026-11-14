// SPDX-License-Identifier: AGPL-3.0-only
//! The serving commit's Stop fence on the real Runner environment (`demo_chain.py:1073-1080 @
//! f4e366e9`: the Stop check and the publication run under `OwnerRun._lock`, which `OwnerRun.stop`
//! writes under). Deterministic: the commit is parked on a channel, never timed.
//! Proves: B-RUN-10 (fenced serving publication), B-RUN-17 (lock boundary).
use super::*;
use serde_json::json;
use std::sync::{
    TryLockError,
    atomic::{AtomicBool, Ordering},
    mpsc,
};

struct NoClocks;
impl Clocks for NoClocks {
    fn mono(&self) -> f64 {
        panic!("the fence reads no clock")
    }
    fn wall(&self) -> f64 {
        panic!("the fence reads no clock")
    }
}

fn shared() -> Arc<Shared> {
    Arc::new(Shared {
        run: Mutex::new(OwnerRun::new(Box::new(|| Identity {
            run_id: "fence-run".into(),
            deployment_id: "fence-deployment".into(),
        }))),
        queue: Mutex::new(Queue::default()),
        wake: Condvar::new(),
        stop: StopEvent {
            set: Mutex::new(false),
            changed: Condvar::new(),
        },
        clocks: Arc::new(NoClocks),
        timing: Timing::registered(),
    })
}

fn patience() -> Duration {
    Duration::from_secs_f64(Timing::registered().rebuild_join)
}

/// Proves: B-RUN-10 (while the serving commit runs, the owner's Stop cannot be set: the stop event
/// is held across the commit, so a Stop lands before the checks or after the publication, never
/// between them; the run and queue locks stay free for status reads).
#[test]
fn a_stop_cannot_land_inside_the_serving_commit() {
    let shared = shared();
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel::<()>();
    let worker = shared.clone();
    let commit = std::thread::spawn(move || {
        let mut env = Env { shared: &worker };
        env.fenced(
            &mut || {
                entered_tx.send(()).expect("test receives the entry");
                release_rx
                    .recv_timeout(patience())
                    .map_err(|_| StepError::Stopped)
            },
            "registry_origin",
            Value::Null,
        )
    });
    entered_rx.recv_timeout(patience()).expect("commit entered");
    let stop_excluded = matches!(shared.stop.set.try_lock(), Err(TryLockError::WouldBlock));
    let run_free = shared.run.try_lock().is_ok();
    let queue_free = shared.queue.try_lock().is_ok();
    release_tx.send(()).expect("commit waits for release");
    let result = commit.join().expect("commit thread");
    assert!(stop_excluded, "a Stop must wait for the serving commit");
    assert!(run_free && queue_free, "status stays answerable");
    assert_eq!(result, Ok(()));
}

/// Proves: B-RUN-10 (a Stop set before the fence ends the step `Stopped` and the commit never runs:
/// `raise _Stopped()` precedes every workflow re-check, demo_chain.py:1077-1080).
#[test]
fn a_stop_before_the_fence_skips_the_commit() {
    let shared = shared();
    shared.stop.assign(true);
    let mut env = Env { shared: &shared };
    let mut ran = false;
    let result = env.fenced(
        &mut || {
            ran = true;
            Ok(())
        },
        "registry_origin",
        Value::Null,
    );
    assert_eq!(result, Err(StepError::Stopped));
    assert!(!ran, "nothing is published after a Stop");
}

/// A run whose first step publishes inside the fence: the commit announces, then parks until the
/// test releases it. Every later step fails the run, so the run ends on its own.
struct Publishing {
    announced: Arc<AtomicBool>,
    entered: mpsc::Sender<()>,
    release: mpsc::Receiver<()>,
    outcome: mpsc::Sender<Result<(), StepError>>,
}
impl Chain for Publishing {
    fn new_run(&mut self, _: &str, _: &str, _: Reason, _: Option<&Value>, _: Option<&Value>) {}
    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        if step != "model" {
            return Err(OwnerRunError::new("test_run_ends_here").into());
        }
        let (announced, entered, release) = (&self.announced, &self.entered, &self.release);
        let result = env.fenced(
            &mut || {
                announced.store(true, Ordering::SeqCst);
                // A failed send means the test already unwound: do not park.
                if entered.send(()).is_ok() {
                    match release.recv_timeout(patience()) {
                        Ok(()) | Err(mpsc::RecvTimeoutError::Disconnected) => {}
                        Err(mpsc::RecvTimeoutError::Timeout) => panic!("test release timed out"),
                    }
                }
                Ok(())
            },
            "registry_origin",
            json!("http://127.0.0.1:4385"),
        );
        assert!(self.outcome.send(result.clone()).is_ok(), "test listens");
        result
    }
    fn teardown(&mut self, _: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        Ok(())
    }
}

/// Proves: B-RUN-10 (the `serving` announcement and its `registry_origin` are ONE transaction for
/// status: v0.1.4 commits the announcement and writes `ctx.result['registry_origin']` inside
/// `with owner._lock` (demo_chain.py:1075-1097), the lock `status_snapshot()` reads under
/// (owner_run.py:194), so no status shows the announcement without its origin. A status read that
/// starts after the announcement waits for the origin.)
#[test]
fn a_status_snapshot_never_shows_the_serving_announcement_without_its_origin() {
    let announced = Arc::new(AtomicBool::new(false));
    let (entered_tx, entered) = mpsc::channel();
    let (release, release_rx) = mpsc::channel::<()>();
    let (outcome_tx, outcome) = mpsc::channel();
    let runner = Runner::spawn(
        Box::new(Publishing {
            announced: announced.clone(),
            entered: entered_tx,
            release: release_rx,
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
    runner.start("start", None).expect("start");
    entered.recv_timeout(patience()).expect("commit entered");
    // The commit is parked right after its announcement.
    let run_held = matches!(runner.shared.run.try_lock(), Err(TryLockError::WouldBlock));
    let view = runner.view();
    let watcher_announced = announced.clone();
    let watcher = std::thread::spawn(move || {
        let after_announcement = watcher_announced.load(Ordering::SeqCst);
        (after_announcement, view.snapshot())
    });
    let (seen_after_announcement, snapshot) = if run_held {
        // The snapshot waits for the transaction: release it first.
        release.send(()).expect("commit waits for release");
        watcher.join().expect("watcher")
    } else {
        let seen = watcher.join().expect("watcher");
        release.send(()).expect("commit waits for release");
        seen
    };
    let published = outcome.recv_timeout(patience()).expect("the step ended");
    runner.close();
    assert_eq!(published, Ok(()));
    assert!(
        seen_after_announcement,
        "the watcher read after the announcement"
    );
    assert_eq!(
        snapshot["registry_origin"],
        json!("http://127.0.0.1:4385"),
        "a status read after the announcement carries its origin: {snapshot}"
    );
    assert!(run_held, "the run lock is held across the publication");
}
