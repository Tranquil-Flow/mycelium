// SPDX-License-Identifier: AGPL-3.0-only
//! Request-keyed prefill watch of the v0.1.4 iroh transport (mycelium_router/transports/iroh.py
//! @ f4e366e9) as a pure, clock-injected core:
//!
//! - [`PrefillProgress`] is `_PrefillProgress` (:810-827);
//! - [`PrefillWatch`] is the transport's bounded LRU of watched requests (`_prefill_progress`,
//!   an `OrderedDict` capped at `_PREFILL_PROGRESS_LIMIT` :129) with `watch_prefill`
//!   (:2346-2364), `release_prefill_watch` (:2366-2368) and `prefill_progress` (:2370-2387);
//! - [`PrefillWatch::frame_deadline`] is the frame deadline ordering of `_send_router_frame`
//!   (:1623-1638): a frame of a watched request expires `ordering_margin_seconds` (2.0 s, :184)
//!   before the prefill wait does.
//!
//! No lock, sidecar, frame decode or send happens here; the transport's send path calls in under
//! its own state lock and owns `delivery_timeout_seconds` (validated by its constructor, :865).
//! Times are the clock's floats, as `time.monotonic()` returns them, and the watch deadline is the
//! caller's f64 as it is (`float()` of a float is that float): NaN, ±inf, negative and huge
//! deadlines are stored and compared exactly as Python does, never refused. The cap and the margin
//! come from the budget table (`IrohPrefillProgressLimit`,
//! `PyMyceliumRouterTransportsIrohPrefillstallpolicyOrderingMarginSecondsL184`).
use budgets::{BudgetId, Entries, Refusal};
use std::collections::{HashMap, VecDeque};

/// The monotonic clock the watch reads (`time.monotonic()`, seconds).
pub trait MonotonicClock {
    fn monotonic(&self) -> f64;
}

impl<T: MonotonicClock + ?Sized> MonotonicClock for &T {
    fn monotonic(&self) -> f64 {
        (**self).monotonic()
    }
}

/// A refusal of the watch, with the oracle exception's own text.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PrefillWatchError {
    /// `ValueError(message)`.
    Value(&'static str),
    /// `IrohTransportError(code)`.
    Transport(&'static str),
}

/// :2353-2354, for an empty request id.
const EMPTY_REQUEST_ID: PrefillWatchError =
    PrefillWatchError::Value("request_id must be a non-empty string");
/// :1637-1638, a watched frame whose ordered deadline is not after the clock.
const DELIVERY_DEADLINE_EXCEEDED: PrefillWatchError =
    PrefillWatchError::Transport("delivery_deadline_exceeded");

/// `_PrefillProgress` (:810-827): one watched request's link progress. The counters belong to
/// the send path (:1937-1943, :1999-2001, :2139-2144, :2533-2545), which updates them in place
/// through [`PrefillWatch::progress_mut`].
#[derive(Clone, Debug, PartialEq)]
pub struct PrefillProgress {
    pub started_at: f64,
    pub last_progress_at: f64,
    /// The node's prefill-wait deadline (monotonic seconds); `None` until a watch sets it.
    pub deadline: Option<f64>,
    pub progress_events: u64,
    pub bytes_confirmed: u64,
    pub max_frame_bytes: u64,
    pub stall_resends: u64,
    pub send_lock_stalls: u64,
    pub failure_code: Option<String>,
}

impl PrefillProgress {
    /// `_PrefillProgress(started_at=now, last_progress_at=now)` (:2359).
    fn started(now: f64) -> Self {
        Self {
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
}

/// The watched requests, least recently watched first.
pub struct PrefillWatch<C> {
    clock: C,
    records: HashMap<String, PrefillProgress>,
    /// The `OrderedDict` order: front is the least recently watched (`popitem(last=False)`).
    order: VecDeque<String>,
    /// `_PREFILL_PROGRESS_LIMIT` (:129).
    limit: usize,
    /// `PrefillStallPolicy.ordering_margin_seconds` (:184) as the oracle's float.
    ordering_margin_seconds: f64,
}

impl<C: MonotonicClock> PrefillWatch<C> {
    /// An empty watch; refuses only when the cap or margin budget row is not of its unit.
    pub fn new(clock: C) -> Result<Self, Refusal> {
        let limit = BudgetId::IrohPrefillProgressLimit.count()?.get();
        let margin =
            BudgetId::PyMyceliumRouterTransportsIrohPrefillstallpolicyOrderingMarginSecondsL184
                .span(Entries::ZERO)?;
        Ok(Self {
            clock,
            records: HashMap::new(),
            order: VecDeque::new(),
            // A cap beyond usize is beyond any length the map can reach: saturating is exact.
            limit: usize::try_from(limit).unwrap_or(usize::MAX),
            // An integral millisecond count over 1000 rounds once, to the double nearest the
            // oracle's decimal literal (2000 / 1000 = 2.0 exactly).
            ordering_margin_seconds: margin.millis() as f64 / 1000.0,
        })
    }

    /// `watch_prefill(request_id, deadline_monotonic=...)` (:2346-2364). A new id gets a fresh
    /// record at the clock and evicts the least recently watched records beyond the cap; every
    /// call, new or repeat, sets the deadline and makes the id the most recent. A repeat keeps
    /// `started_at`, `last_progress_at` and every counter. An empty id is refused before the
    /// clock is read or anything changes; any other string (whitespace, non-ASCII, long) is
    /// watched.
    pub fn watch_prefill(
        &mut self,
        request_id: &str,
        deadline_monotonic: f64,
    ) -> Result<(), PrefillWatchError> {
        if request_id.is_empty() {
            return Err(EMPTY_REQUEST_ID);
        }
        let now = self.clock.monotonic();
        if let Some(progress) = self.records.get_mut(request_id) {
            progress.deadline = Some(deadline_monotonic);
            self.move_to_end(request_id);
            return Ok(());
        }
        let mut progress = PrefillProgress::started(now);
        progress.deadline = Some(deadline_monotonic);
        self.records.insert(request_id.to_owned(), progress);
        // Appended at the most recent end, so eviction from the front never reaches it while
        // the cap is at least one.
        self.order.push_back(request_id.to_owned());
        while self.order.len() > self.limit {
            match self.order.pop_front() {
                Some(oldest) => {
                    self.records.remove(&oldest);
                }
                None => break,
            }
        }
        Ok(())
    }

    /// `release_prefill_watch` (:2366-2368): drops the record if present; an absent id is a
    /// no-op success.
    pub fn release_prefill_watch(&mut self, request_id: &str) {
        if self.records.remove(request_id).is_some() {
            self.order.retain(|id| id != request_id);
        }
    }

    /// `prefill_progress` (:2370-2387): a snapshot of a watched record, `None` when absent.
    pub fn prefill_progress(&self, request_id: &str) -> Option<PrefillProgress> {
        self.records.get(request_id).cloned()
    }

    /// The record itself for the send path's in-place updates (`_prefill_progress.get`); it
    /// does not change the LRU order.
    pub fn progress_mut(&mut self, request_id: &str) -> Option<&mut PrefillProgress> {
        self.records.get_mut(request_id)
    }

    /// The watched request ids, least recently watched first.
    pub fn watched(&self) -> impl Iterator<Item = &str> {
        self.order.iter().map(String::as_str)
    }

    pub fn len(&self) -> usize {
        self.order.len()
    }

    pub fn is_empty(&self) -> bool {
        self.order.is_empty()
    }

    /// The absolute deadline of one frame (:1623-1638): the clock plus the delivery timeout,
    /// ordered before a watched request's deadline by the margin with Python's
    /// `min(deadline, watched - margin)` (the second only when strictly smaller, so a NaN on
    /// either side keeps the first). Only a watched request with a deadline is then checked
    /// against a second clock read, and refused when its deadline is not after it.
    pub fn frame_deadline(
        &self,
        request_id: Option<&str>,
        delivery_timeout_seconds: f64,
    ) -> Result<f64, PrefillWatchError> {
        let mut deadline = self.clock.monotonic() + delivery_timeout_seconds;
        let watched = request_id
            .and_then(|id| self.records.get(id))
            .and_then(|progress| progress.deadline);
        if let Some(watched) = watched {
            let ordered = watched - self.ordering_margin_seconds;
            if ordered < deadline {
                deadline = ordered;
            }
            if deadline <= self.clock.monotonic() {
                return Err(DELIVERY_DEADLINE_EXCEEDED);
            }
        }
        Ok(deadline)
    }

    /// `OrderedDict.move_to_end` (:2364).
    fn move_to_end(&mut self, request_id: &str) {
        if let Some(at) = self.order.iter().position(|id| id == request_id)
            && let Some(id) = self.order.remove(at)
        {
            self.order.push_back(id);
        }
    }
}
