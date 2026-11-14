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
        stop: StopEvent::new(),
        clocks: Arc::new(NoClocks),
        timing: Timing::registered(),
    })
}

/// A machine whose run context 1 is running, as the driver sees it at its first step.
fn started() -> (Arc<Shared>, u64) {
    let shared = shared();
    let context = {
        let mut run = lock(&shared.run);
        let cmds = run
            .start(
                "start",
                None,
                Clock {
                    mono: 0.0,
                    wall: 0.0,
                },
            )
            .expect("start");
        let mut queue = lock(&shared.queue);
        shared.dispatch(run.generation(), &mut queue, cmds);
        run.generation()
    };
    (shared, context)
}

fn patience() -> Duration {
    Duration::from_secs_f64(Timing::registered().rebuild_join)
}

/// Proves: B-RUN-10 (while the serving commit runs, the owner's Stop cannot land: the run lock,
/// which every Stop takes first (`OwnerRun.stop` writes under `OwnerRun._lock`, owner_run.py:275-282)
/// is held across the commit, as v0.1.4 holds `owner._lock` across it (demo_chain.py:1075-1097),
/// so a Stop lands before the checks or after the publication, never between them; the queue
/// stays free).
#[test]
fn a_stop_cannot_land_inside_the_serving_commit() {
    let (shared, context) = started();
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel::<()>();
    let worker = shared.clone();
    let commit = std::thread::spawn(move || {
        let mut env = Env {
            shared: &worker,
            context,
        };
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
    let stop_excluded = matches!(shared.run.try_lock(), Err(TryLockError::WouldBlock));
    let queue_free = shared.queue.try_lock().is_ok();
    release_tx.send(()).expect("commit waits for release");
    let result = commit.join().expect("commit thread");
    assert!(stop_excluded, "a Stop must wait for the serving commit");
    assert!(queue_free, "the commit holds no queue lock");
    assert_eq!(result, Ok(()));
}

/// Proves: B-RUN-10 (a Stop set before the fence ends the step `Stopped` and the commit never runs:
/// `raise _Stopped()` precedes every workflow re-check, demo_chain.py:1077-1080).
#[test]
fn a_stop_before_the_fence_skips_the_commit() {
    let (shared, context) = started();
    shared.stop.assign(true);
    let mut env = Env {
        shared: &shared,
        context,
    };
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
/// test releases it. Every later step fails the run, so the run ends on its own; with
/// `hold_until_stop` the publishing step instead waits for the run's Stop after publishing.
struct Publishing {
    announced: Arc<AtomicBool>,
    entered: mpsc::Sender<()>,
    release: mpsc::Receiver<()>,
    outcome: mpsc::Sender<Result<(), StepError>>,
    hold_until_stop: bool,
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
                    // Released, or the sender dropped (the test unwound): both end the park.
                    let timed_out = matches!(
                        release.recv_timeout(patience()),
                        Err(mpsc::RecvTimeoutError::Timeout)
                    );
                    assert!(!timed_out, "test release timed out");
                }
                Ok(())
            },
            "registry_origin",
            json!("http://127.0.0.1:4385"),
        );
        assert!(self.outcome.send(result.clone()).is_ok(), "test listens");
        if self.hold_until_stop && result.is_ok() {
            while !env.pause(Timing::registered().poll.as_secs_f64()) {}
            return Err(StepError::Stopped);
        }
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
            hold_until_stop: false,
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

/// Proves: B-RUN-10 (REVIEW-laneSRV SRV-8, behaviour, not mechanism: a real `Runner::stop()` issued
/// while the serving commit is parked lands AFTER the publication, as `OwnerRun.stop` waits for
/// `owner._lock` that the commit holds (demo_chain.py:1075-1097, owner_run.py:275-282): the step's
/// publication is `Ok`, and the Stop's own answer already carries the origin.)
#[test]
fn a_concurrent_stop_lands_after_the_parked_publication() {
    let announced = Arc::new(AtomicBool::new(false));
    let (entered_tx, entered) = mpsc::channel();
    let (release, release_rx) = mpsc::channel::<()>();
    let (outcome_tx, outcome) = mpsc::channel();
    let runner = Runner::spawn(
        Box::new(Publishing {
            announced,
            entered: entered_tx,
            release: release_rx,
            outcome: outcome_tx,
            hold_until_stop: true,
        }),
        Box::new(|| Identity {
            run_id: "stop-run".into(),
            deployment_id: "stop-deployment".into(),
        }),
        Arc::new(SystemClocks::new()),
        Timing::registered(),
    )
    .expect("runner");
    runner.start("start", None).expect("start");
    entered.recv_timeout(patience()).expect("commit entered");
    let run_held = matches!(runner.shared.run.try_lock(), Err(TryLockError::WouldBlock));
    let answer = std::thread::scope(|scope| {
        let stopper = scope.spawn(|| runner.stop());
        release.send(()).expect("commit waits for release");
        stopper.join().expect("stopper")
    });
    let published = outcome.recv_timeout(patience()).expect("the step ended");
    runner.close();
    assert!(run_held, "the commit was parked under the run lock");
    assert_eq!(published, Ok(()), "the parked publication completes");
    assert_eq!(answer["state"], "stopping", "{answer}");
    assert_eq!(
        answer["registry_origin"],
        json!("http://127.0.0.1:4385"),
        "the Stop landed after the publication: {answer}"
    );
}
