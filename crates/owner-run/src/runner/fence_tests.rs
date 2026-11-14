// SPDX-License-Identifier: AGPL-3.0-only
//! The serving commit's Stop fence on the real Runner environment (`demo_chain.py:1073-1080 @
//! f4e366e9`: the Stop check and the publication run under `OwnerRun._lock`, which `OwnerRun.stop`
//! writes under). Deterministic: the commit is parked on a channel, never timed.
//! Proves: B-RUN-10 (fenced serving publication), B-RUN-17 (lock boundary).
use super::*;
use std::sync::{TryLockError, mpsc};

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
        env.fenced(&mut || {
            entered_tx.send(()).expect("test receives the entry");
            release_rx
                .recv_timeout(patience())
                .map_err(|_| StepError::Stopped)
        })
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
    let result = env.fenced(&mut || {
        ran = true;
        Ok(())
    });
    assert_eq!(result, Err(StepError::Stopped));
    assert!(!ran, "nothing is published after a Stop");
}
