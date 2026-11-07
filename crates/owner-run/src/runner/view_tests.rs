// SPDX-License-Identifier: AGPL-3.0-only
//! Deterministic lock-boundary tests for SE-SLICE1-RUNNER-VIEW. These test the new Rust
//! host interface, not new oracle values. Public lifecycle tests live in tests/runner_view.rs.
//! Proves: B-RUN-17 (snapshot boundary), B-RUN-18 (atomic inactive callback only).
//! Host publication, signed announcements and full behavior acceptance remain consumer gates.
use super::*;
use std::sync::{TryLockError, mpsc};

struct NoObservers;
impl Clocks for NoObservers {
    fn mono(&self) -> f64 {
        panic!("a view must not call an injected clock")
    }
    fn wall(&self) -> f64 {
        panic!("a view must not call an injected clock")
    }
}

fn shared() -> Arc<Shared> {
    Arc::new(Shared {
        run: Mutex::new(OwnerRun::new(Box::new(|| Identity {
            run_id: "view-run".into(),
            deployment_id: "view-deployment".into(),
        }))),
        queue: Mutex::new(Queue::default()),
        wake: Condvar::new(),
        stop: StopEvent {
            set: Mutex::new(false),
            changed: Condvar::new(),
        },
        clocks: Arc::new(NoObservers),
        timing: Timing::registered(),
    })
}

fn patience() -> Duration {
    Duration::from_secs_f64(Timing::registered().rebuild_join)
}

#[test]
fn inactive_callback_excludes_transition_and_holds_no_queue_lock() {
    let shared = shared();
    let view = RunnerView {
        shared: shared.clone(),
    };
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let callback = std::thread::spawn(move || {
        view.while_inactive(|| {
            entered_tx.send(()).unwrap();
            release_rx.recv_timeout(patience()).unwrap();
            "refreshed"
        })
    });
    entered_rx.recv_timeout(patience()).unwrap();
    // No sleep or scheduling assumption: the callback is held until this thread releases it.
    let transition_excluded = matches!(shared.run.try_lock(), Err(TryLockError::WouldBlock));
    let queue_free = shared.queue.try_lock().is_ok();
    release_tx.send(()).unwrap();
    let answer = callback.join().unwrap();
    assert!(
        transition_excluded,
        "inactive check and callback must retain the run lock atomically"
    );
    assert!(queue_free, "callback must not retain the queue lock");
    assert_eq!(answer, Some("refreshed"));
}

#[test]
fn held_transition_finishes_before_inactive_decision() {
    let shared = shared();
    let view = RunnerView {
        shared: shared.clone(),
    };
    let mut run = lock(&shared.run);
    let (ready_tx, ready_rx) = mpsc::channel();
    let callback = std::thread::spawn(move || {
        ready_tx.send(()).unwrap();
        view.while_inactive(|| panic!("refresh must not run after the held start"))
    });
    ready_rx.recv_timeout(patience()).unwrap();
    // The operation overlaps an explicitly held transition; commit Start before releasing it.
    assert!(
        !run.start(
            "start",
            None,
            Clock {
                mono: 0.0,
                wall: 0.0
            }
        )
        .unwrap()
        .is_empty()
    );
    drop(run);
    assert_eq!(callback.join().unwrap(), None);
}

#[test]
fn view_reads_and_idle_operation_do_not_acquire_queue_or_call_observers() {
    let shared = shared();
    let view = RunnerView {
        shared: shared.clone(),
    };
    let queue = lock(&shared.queue);
    let (done_tx, done_rx) = mpsc::channel();
    let reader = std::thread::spawn(move || {
        let snapshot = view.snapshot();
        let active = view.active();
        let answer = view.while_inactive(|| "idle refresh");
        done_tx.send((snapshot, active, answer)).unwrap();
    });
    let result = done_rx.recv_timeout(patience());
    // Always release before joining, even if a faulty view tried to take queue.
    drop(queue);
    reader.join().unwrap();
    let (snapshot, active, answer) = result.expect("view must not wait on queue");
    assert_eq!(snapshot["state"], "idle");
    assert!(!active);
    assert_eq!(answer, Some("idle refresh"));
}

#[test]
fn active_operation_is_a_noop_and_does_not_call_observers() {
    let shared = shared();
    let view = RunnerView {
        shared: shared.clone(),
    };
    assert!(
        !lock(&shared.run)
            .start(
                "start",
                None,
                Clock {
                    mono: 0.0,
                    wall: 0.0
                }
            )
            .unwrap()
            .is_empty()
    );
    assert!(view.active());
    assert_eq!(view.snapshot()["state"], "running");
    assert_eq!(view.while_inactive(|| panic!("active callback ran")), None);
}
