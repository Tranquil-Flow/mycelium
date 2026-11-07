// SPDX-License-Identifier: AGPL-3.0-only
//! Request-keyed prefill watch core (`runtime::prefill_watch`) against v0.1.4
//! mycelium_router/transports/iroh.py @ f4e366e9: `watch_prefill` :2346-2364,
//! `release_prefill_watch` :2366-2368, `prefill_progress` :2370-2387, the frame deadline ordering
//! of `_send_router_frame` :1623-1638 and `_PREFILL_PROGRESS_LIMIT = 256` :129. The ordering
//! margin is `PrefillStallPolicy.ordering_margin_seconds = 2.0` (:184). Every instant, record and
//! refusal below is pinned by hand from those lines on a fake clock; none is computed by the core
//! or read through its budget accessor.
use budgets::table::{Provenance, Value};
use budgets::{BudgetId, Entries, Span};
use runtime::prefill_watch::{MonotonicClock, PrefillProgress, PrefillWatch, PrefillWatchError};
use std::cell::Cell;

/// A settable monotonic clock (seconds).
struct Clock(Cell<f64>);

impl Clock {
    fn at(seconds: f64) -> Self {
        Self(Cell::new(seconds))
    }
    fn set(&self, seconds: f64) {
        self.0.set(seconds);
    }
}

impl MonotonicClock for Clock {
    fn monotonic(&self) -> f64 {
        self.0.get()
    }
}

/// A clock that moves `step` seconds after every read.
struct Ticking {
    now: Cell<f64>,
    step: f64,
}

impl MonotonicClock for Ticking {
    fn monotonic(&self) -> f64 {
        let now = self.now.get();
        self.now.set(now + self.step);
        now
    }
}

fn core(clock: &Clock) -> PrefillWatch<&Clock> {
    PrefillWatch::new(clock).expect("the prefill watch budgets are declared")
}

/// `IrohTransportError("delivery_deadline_exceeded")` (:1637-1638).
const EXCEEDED: PrefillWatchError = PrefillWatchError::Transport("delivery_deadline_exceeded");

/// A fresh `_PrefillProgress(started_at=now, last_progress_at=now)` (:2359): every counter 0,
/// no failure code and no deadline until the same call sets it (:2363).
fn fresh(now: f64) -> PrefillProgress {
    PrefillProgress {
        started_at: now,
        last_progress_at: now,
        deadline: None,
        progress_events: 0,
        bytes_confirmed: 0,
        max_frame_bytes: 0,
        stall_resends: 0,
        send_lock_stalls: 0,
        failure_code: None,
    }
}

#[test]
fn prefill_watch_core_frame_deadline_is_min() {
    let margin =
        BudgetId::PyMyceliumRouterTransportsIrohPrefillstallpolicyOrderingMarginSecondsL184;
    assert_eq!(
        margin.entry().value,
        Value::Duration(Span::from_millis(2_000))
    );
    assert_eq!(
        margin.entry().provenance,
        Provenance::Source("mycelium_router/transports/iroh.py:184 @ f5bfb709")
    );
    let clock = Clock::at(100.0);
    let mut watch = core(&clock);
    let delivery = 30.0;
    let (soon, late) = (110.0, 140.0);
    assert_eq!(watch.watch_prefill("r-soon", soon), Ok(()));
    assert_eq!(watch.watch_prefill("r-late", late), Ok(()));
    // :1627 100 + 30 = 130; :1633-1636 min(130, 110 - 2) = 108: the watch wins.
    assert_eq!(watch.frame_deadline(Some("r-soon"), delivery), Ok(108.0));
    // min(130, 140 - 2 = 138) = 130: the delivery timeout wins.
    assert_eq!(watch.frame_deadline(Some("r-late"), delivery), Ok(130.0));
    // Python min(a, b) is b only when b < a. A NaN watch deadline gives nan - 2 = nan, never
    // smaller, so 130 stays and 130 <= 100 is false. +inf - 2 = +inf is not smaller either.
    // -inf - 2 = -inf is, and -inf <= 100 raises.
    let (nan, inf) = (f64::NAN, f64::INFINITY);
    assert_eq!(watch.watch_prefill("r-nan", nan), Ok(()));
    assert_eq!(watch.frame_deadline(Some("r-nan"), delivery), Ok(130.0));
    assert_eq!(watch.watch_prefill("r-inf", inf), Ok(()));
    assert_eq!(watch.frame_deadline(Some("r-inf"), delivery), Ok(130.0));
    assert_eq!(watch.watch_prefill("r-neg-inf", -inf), Ok(()));
    assert_eq!(
        watch.frame_deadline(Some("r-neg-inf"), delivery),
        Err(EXCEEDED)
    );
    // A NaN first argument is kept: 108 < nan is false, so min(nan, 108) is nan, and nan <= 100
    // is false: the frame carries the NaN deadline.
    let nan_delivery = watch.frame_deadline(Some("r-soon"), nan);
    assert!(
        matches!(nan_delivery, Ok(deadline) if deadline.is_nan()),
        "{nan_delivery:?}"
    );
}

#[test]
fn prefill_watch_core_equal_boundary_expires() {
    let clock = Clock::at(100.0);
    let mut watch = core(&clock);
    let delivery = 30.0;
    let (at_margin, above_margin) = (102.0, 102.00000000000001);
    assert_eq!(watch.watch_prefill("r-boundary", at_margin), Ok(()));
    // min(130, 102 - 2) = 100 and 100 <= 100 (:1637): exactly delivery_deadline_exceeded.
    assert_eq!(
        watch.frame_deadline(Some("r-boundary"), delivery),
        Err(EXCEEDED)
    );
    // One ulp later (102 + 2^-46) leaves 100 + 2^-46 > 100: the frame may still go.
    assert_eq!(watch.watch_prefill("r-above", above_margin), Ok(()));
    assert_eq!(
        watch.frame_deadline(Some("r-above"), delivery),
        Ok(100.00000000000001)
    );
    // The expiry check reads the clock again (:1637), after the read of :1627. On a clock that
    // moves 1 s per read, the watch reads 100; the frame reads 101 (131 vs 103.5 - 2 = 101.5,
    // so 101.5) and then 102: 101.5 <= 102 is exceeded, although 101.5 is after the first read.
    let ticking = Ticking {
        now: Cell::new(100.0),
        step: 1.0,
    };
    let mut moving = PrefillWatch::new(&ticking).expect("the prefill watch budgets are declared");
    let between_reads = 103.5;
    assert_eq!(moving.watch_prefill("r-moving", between_reads), Ok(()));
    assert_eq!(
        moving.frame_deadline(Some("r-moving"), delivery),
        Err(EXCEEDED)
    );
}

#[test]
fn prefill_watch_core_absent_watch_uses_delivery_timeout() {
    let clock = Clock::at(100.0);
    let mut watch = core(&clock);
    let delivery = 30.0;
    // :1627 100 + 30 = 130 with no watch record (:1630-1632 get() is None).
    assert_eq!(watch.frame_deadline(Some("r-absent"), delivery), Ok(130.0));
    // A frame without a request scope never looks (:1628).
    assert_eq!(watch.frame_deadline(None, delivery), Ok(130.0));
    // A record whose deadline is None leaves the plain delivery timeout (:1633).
    let watched = 110.0;
    assert_eq!(watch.watch_prefill("r-unset", watched), Ok(()));
    watch
        .progress_mut("r-unset")
        .expect("a watched record")
        .deadline = None;
    assert_eq!(watch.frame_deadline(Some("r-unset"), delivery), Ok(130.0));
    // A released watch no longer orders the frame.
    assert_eq!(watch.watch_prefill("r-released", watched), Ok(()));
    watch.release_prefill_watch("r-released");
    assert_eq!(
        watch.frame_deadline(Some("r-released"), delivery),
        Ok(130.0)
    );
    // Only a watched request with a deadline is checked against the clock (:1633-1638). A
    // positive delivery timeout below the clock's ulp (the constructor only refuses <= 0, :865)
    // gives 100 + 1e-20 = 100: unchecked, the plain deadline is kept, even for the record whose
    // deadline is None; a watched request is exceeded (min(100, 110 - 2) = 100 <= 100).
    let tiny = 1e-20;
    assert_eq!(watch.frame_deadline(Some("r-absent"), tiny), Ok(100.0));
    assert_eq!(watch.frame_deadline(None, tiny), Ok(100.0));
    assert_eq!(watch.frame_deadline(Some("r-unset"), tiny), Ok(100.0));
    assert_eq!(watch.watch_prefill("r-watched", watched), Ok(()));
    assert_eq!(watch.frame_deadline(Some("r-watched"), tiny), Err(EXCEEDED));
}

#[test]
fn prefill_watch_core_repeat_updates_deadline() {
    let clock = Clock::at(100.0);
    let mut watch = core(&clock);
    let (first, second, delivery) = (110.0, 120.0, 30.0);
    assert_eq!(watch.watch_prefill("r1", first), Ok(()));
    assert_eq!(
        watch.prefill_progress("r1"),
        Some(PrefillProgress {
            deadline: Some(110.0),
            ..fresh(100.0)
        })
    );
    // The send path's counters (:1937-1943, :1999-2001, :2139-2144, :2533-2545) are seeded in place.
    let record = watch.progress_mut("r1").expect("a watched record");
    record.progress_events = 3;
    record.bytes_confirmed = 4096;
    record.max_frame_bytes = 2048;
    record.stall_resends = 1;
    record.send_lock_stalls = 2;
    record.failure_code = None;
    clock.set(101.0);
    // :2357-2363 a repeat only sets the deadline: started_at, last_progress_at and every
    // counter stay.
    assert_eq!(watch.watch_prefill("r1", second), Ok(()));
    let seeded = PrefillProgress {
        started_at: 100.0,
        last_progress_at: 100.0,
        deadline: Some(120.0),
        progress_events: 3,
        bytes_confirmed: 4096,
        max_frame_bytes: 2048,
        stall_resends: 1,
        send_lock_stalls: 2,
        failure_code: None,
    };
    assert_eq!(watch.prefill_progress("r1"), Some(seeded));
    // 101 + 30 = 131; min(131, 120 - 2) = 118.
    assert_eq!(watch.frame_deadline(Some("r1"), delivery), Ok(118.0));
    // A failure code is a field of the record too, kept by a repeat.
    watch
        .progress_mut("r1")
        .expect("a watched record")
        .failure_code = Some("send_failed".to_string());
    assert_eq!(watch.watch_prefill("r1", second), Ok(()));
    assert_eq!(
        watch
            .prefill_progress("r1")
            .and_then(|record| record.failure_code),
        Some("send_failed".to_string())
    );
    // After a release the id is new again (:2358-2360): a fresh record at the current clock.
    watch.release_prefill_watch("r1");
    clock.set(102.0);
    assert_eq!(watch.watch_prefill("r1", second), Ok(()));
    assert_eq!(
        watch.prefill_progress("r1"),
        Some(PrefillProgress {
            deadline: Some(120.0),
            ..fresh(102.0)
        })
    );
}

#[test]
fn prefill_watch_core_lru_evicts_oldest() {
    let cap = BudgetId::IrohPrefillProgressLimit;
    assert_eq!(cap.entry().value, Value::Count(Entries::new(256)));
    assert_eq!(
        cap.entry().provenance,
        Provenance::Source("mycelium_router/transports/iroh.py:129 @ f4e366e9")
    );
    let clock = Clock::at(100.0);
    let mut watch = core(&clock);
    let deadline = 160.0;
    let id = |n: u32| format!("r{n:03}");
    for n in 1..=256 {
        assert_eq!(watch.watch_prefill(&id(n), deadline), Ok(()));
    }
    assert_eq!(watch.len(), 256);
    // A repeat moves r001 to the most recent end (:2364); the 257th id then evicts the least
    // recent one, r002 (:2361-2362 popitem(last=False)).
    assert_eq!(watch.watch_prefill("r001", deadline), Ok(()));
    assert_eq!(watch.watch_prefill("r257", deadline), Ok(()));
    assert_eq!(watch.len(), 256);
    assert_eq!(watch.prefill_progress("r002"), None);
    let mut kept: Vec<String> = (3..=256).map(id).collect();
    kept.push(id(1));
    kept.push(id(257));
    assert_eq!(watch.watched().collect::<Vec<_>>(), kept);
    for name in &kept {
        assert_eq!(
            watch.prefill_progress(name),
            Some(PrefillProgress {
                deadline: Some(160.0),
                ..fresh(100.0)
            }),
            "{name}"
        );
    }
    // :2366-2368 release pops only that record; releasing it again is a no-op success.
    watch.release_prefill_watch("r001");
    kept.retain(|name| name != "r001");
    assert_eq!(watch.len(), 255);
    assert_eq!(watch.prefill_progress("r001"), None);
    assert_eq!(watch.watched().collect::<Vec<_>>(), kept);
    watch.release_prefill_watch("r001");
    assert_eq!(watch.len(), 255);
    assert_eq!(watch.watched().collect::<Vec<_>>(), kept);
}

#[test]
fn prefill_watch_core_empty_request_id_refused() {
    let clock = Clock::at(100.0);
    let mut watch = core(&clock);
    let (deadline, other, delivery) = (110.0, 120.0, 30.0);
    assert_eq!(watch.watch_prefill("r-valid", deadline), Ok(()));
    // :2353-2354 ValueError with the oracle's message, before anything changes.
    assert_eq!(
        watch.watch_prefill("", other),
        Err(PrefillWatchError::Value(
            "request_id must be a non-empty string"
        ))
    );
    assert_eq!(watch.len(), 1);
    assert_eq!(watch.watched().collect::<Vec<_>>(), ["r-valid"]);
    assert_eq!(
        watch.prefill_progress("r-valid"),
        Some(PrefillProgress {
            deadline: Some(110.0),
            ..fresh(100.0)
        })
    );
    assert_eq!(watch.prefill_progress(""), None);
    assert_eq!(watch.frame_deadline(Some("r-valid"), delivery), Ok(108.0));
    // Only emptiness is refused: a whitespace-only id is a non-empty str and is watched.
    assert_eq!(watch.watch_prefill(" ", other), Ok(()));
    assert_eq!(
        watch.prefill_progress(" "),
        Some(PrefillProgress {
            deadline: Some(120.0),
            ..fresh(100.0)
        })
    );
    assert_eq!(watch.watched().collect::<Vec<_>>(), ["r-valid", " "]);
}
