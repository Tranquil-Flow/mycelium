// SPDX-License-Identifier: AGPL-3.0-only
//! Owned carrier shutdown plumbing; no timeout worker or detached joins.
use super::*;
use std::{ops::Deref, thread::JoinHandle, time::Duration};

/// The owner's existing first-Stop timestamp and allowance, never a renewed timer.
#[derive(Clone)]
pub struct StopLimit {
    pub(crate) stopped: Arc<AtomicBool>,
    pub(crate) first: Arc<Mutex<Option<Instant>>>,
    pub(crate) allowance: Duration,
}
impl StopLimit {
    pub fn new(
        stopped: Arc<AtomicBool>,
        first: Arc<Mutex<Option<Instant>>>,
        allowance: Duration,
    ) -> Self {
        Self {
            stopped,
            first,
            allowance,
        }
    }
    pub fn end(&self) -> Option<Instant> {
        self.first
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .map(|at| at.checked_add(self.allowance).unwrap_or(at))
    }
}
#[derive(Default)]
pub(super) struct Shutdown {
    pub limit: Option<StopLimit>,
    pub end: Mutex<Option<Instant>>,
    pub cancel: Arc<AtomicBool>,
    pub finished: AtomicBool,
    pub thread: Mutex<Option<JoinHandle<()>>>,
    pub(super) peers: Mutex<Vec<Connection>>,
}
impl Shutdown {
    pub fn setup_stopped(&self) -> bool {
        self.cancel.load(Ordering::SeqCst)
            || self
                .limit
                .as_ref()
                .is_some_and(|s| s.stopped.load(Ordering::SeqCst))
    }
    pub fn bound(&self, fallback: Instant) -> Instant {
        let own = *self.end.lock().unwrap_or_else(|p| p.into_inner());
        own.into_iter()
            .chain(self.limit.as_ref().and_then(StopLimit::end))
            .fold(fallback, Instant::min)
    }
    pub fn deadline(&self, clock: &impl Clock, budget: B) -> Result<MonoInstant, &'static str> {
        let span = budget
            .span(Entries::ZERO)
            .map_err(|_| "budget_unavailable")?;
        let now = Instant::now();
        let end = self.bound(now + Duration::from_millis(span.millis()));
        let left = u64::try_from(end.saturating_duration_since(now).as_millis())
            .map_err(|_| "deadline_invalid")?;
        clock
            .monotonic()
            .checked_add(budgets::Span::from_millis(left))
            .map_err(|_| "deadline_invalid")
    }
    pub fn keep(&self, connection: &Connection) {
        let mut peers = self.peers.lock().unwrap_or_else(|p| p.into_inner());
        if self.cancel.load(Ordering::SeqCst) {
            connection.close(0u32.into(), b"owner stop");
        }
        peers.retain(|c| c.close_reason().is_none());
        peers.push(connection.clone());
    }
    pub fn wake_io(&self) {
        self.cancel.store(true, Ordering::SeqCst);
        let mut peers = self.peers.lock().unwrap_or_else(|p| p.into_inner());
        for c in peers.drain(..) {
            c.close(0u32.into(), b"owner stop");
        }
    }
    pub fn join(&self) -> Result<(), PublicError> {
        let thread = self.thread.lock().unwrap_or_else(|p| p.into_inner()).take();
        if let Some(thread) = thread {
            thread
                .join()
                .map_err(|_| error("joined_route_carrier_panicked"))?;
        }
        Ok(())
    }
}

pub(super) struct Finished(pub RouteHandle);
impl Drop for Finished {
    fn drop(&mut self) {
        let _state = self.0.state.lock().unwrap_or_else(|p| p.into_inner());
        self.0.shutdown.finished.store(true, Ordering::SeqCst);
        self.0.changed.notify_all();
    }
}

/// Every bind success owns endpoint close, including partial-setup error exits.
/// The borrowed runtime is dropped by the carrier AFTER this guard.
pub(super) struct OwnedHost<'a> {
    pub inner: LoopbackHost<'a>,
    pub observed: RouteHandle,
    pub clock: &'a app_host_clock::Clock,
}
impl<'a> Deref for OwnedHost<'a> {
    type Target = LoopbackHost<'a>;
    fn deref(&self) -> &Self::Target {
        &self.inner
    }
}
impl Drop for OwnedHost<'_> {
    fn drop(&mut self) {
        self.observed.shutdown.wake_io();
        let closed = self
            .observed
            .shutdown
            .deadline(self.clock, B::RouterClose)
            .map_err(str::to_owned)
            .and_then(|end| {
                self.inner
                    .close(self.clock, end)
                    .map_err(|e| format!("joined_route_close_failed:{e:?}"))
            });
        let mut state = self
            .observed
            .state
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        state.carrier_closed = closed.is_ok();
        if let Err(code) = closed {
            eprintln!("{code}");
            state.setup_failure.get_or_insert(code);
        }
    }
}

/// Close under the caller's original end, wake any held I/O at that end, and
/// join the actual carrier thread. A missed end is explicit, not false proof.
pub fn begin_close_until(handle: &RouteHandle, end: Instant) -> Result<(), PublicError> {
    {
        let mut held = handle
            .shutdown
            .end
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        *held = Some(held.map_or(end, |old| old.min(end)));
    }
    close(handle)
}

pub fn close_until(handle: &RouteHandle, end: Instant) -> Result<(), PublicError> {
    begin_close_until(handle, end)?;
    // Constructor-only test doubles have no carrier to join.
    if handle
        .shutdown
        .thread
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .is_none()
    {
        return Ok(());
    }
    let mut state = handle.state.lock().unwrap_or_else(|p| p.into_inner());
    while !handle.shutdown.finished.load(Ordering::SeqCst) {
        let left = handle
            .shutdown
            .bound(end)
            .saturating_duration_since(Instant::now());
        if left.is_zero() {
            break;
        }
        state = handle
            .changed
            .wait_timeout(state, left)
            .unwrap_or_else(|p| p.into_inner())
            .0;
    }
    let expired = !handle.shutdown.finished.load(Ordering::SeqCst);
    drop(state);
    if expired {
        handle.shutdown.wake_io();
        handle.changed.notify_all();
        close(handle)?;
    }
    handle.shutdown.join()?;
    let state = handle.state.lock().unwrap_or_else(|p| p.into_inner());
    if expired || !state.carrier_closed {
        Err(error("owner_target_cleanup_unproven"))
    } else {
        Ok(())
    }
}
