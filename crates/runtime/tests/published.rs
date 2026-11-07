// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-FAIL-11 (F5: status is readable while a slow effect is blocked); B-FAIL-07 (partial: a delayed old generation cannot overwrite the successor snapshot).
//!
//! O:F5 barrier control; no timing sleeps, HTTP server or model work.
use runtime::published::*;
use std::sync::{Arc, Barrier};
#[test]
fn f5_status_is_readable_while_slow_effect_remains_blocked() {
    let state = Arc::new(Published::new(1, "idle"));
    let entered = Arc::new(Barrier::new(2));
    let release = Arc::new(Barrier::new(2));
    let (s, a, b) = (state.clone(), entered.clone(), release.clone());
    let worker = std::thread::spawn(move || {
        s.run(1, "compiling", || {
            a.wait();
            b.wait();
            (Err::<(), _>("compile_failed"), "failed")
        })
    });
    entered.wait();
    let observed = state.try_snapshot();
    release.wait();
    let result = worker.join().unwrap().unwrap();
    assert_eq!(
        observed,
        Ok(Snapshot {
            generation: 1,
            value: "compiling"
        })
    );
    assert_eq!(result, (Err("compile_failed"), Ok(())));
    assert_eq!(state.try_snapshot().unwrap().value, "failed");
}
#[test]
fn delayed_old_generation_cannot_overwrite_successor_snapshot() {
    let state = Published::new(1, "idle");
    let (_, publication) = state
        .run(1, "working", || {
            // try_snapshot first lets the broken locking model fail, not deadlock.
            if state.try_snapshot().is_ok() {
                state.replace(2, "successor").unwrap();
            }
            ((), "old-finished")
        })
        .unwrap();
    assert_eq!(publication, Err(PublishError::StaleGeneration));
    assert_eq!(
        state.try_snapshot().unwrap(),
        Snapshot {
            generation: 2,
            value: "successor"
        }
    );
    assert!(
        state
            .run::<()>(1, "stale", || panic!("old work dispatched"))
            .is_err()
    );
}
