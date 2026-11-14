// SPDX-License-Identifier: AGPL-3.0-only
//! Test-only observation of the Runner's own lock helper ([`super::lock`]), so schedule tests
//! are decided by facts, never by timing:
//! - [`Blocked`]: which threads found a watched lock HELD when they came to take it. A reader
//!   recorded there cannot have read before the holder released the lock, so a test can prove a
//!   status read or a Stop is waiting behind a parked commit before it releases the commit.
//! - [`Gate`]: parks every thread with a given name before it takes a given lock, until the
//!   test releases that thread (the rebuild watcher, whose loop starts with the run lock).
//!
//! Both are keyed by the lock's address and removed (and every parked thread released) when the
//! test's guard drops, also while a failing assertion unwinds.
use std::sync::{Condvar, Mutex, MutexGuard, OnceLock, PoisonError, TryLockError};
use std::thread::ThreadId;
use std::time::{Duration, Instant};

#[derive(Default)]
struct Hooks {
    /// Watched lock addresses and, per address, the threads that found it held.
    blocked: Vec<(usize, Vec<ThreadId>)>,
    gates: Vec<GateState>,
}
struct GateState {
    lock: usize,
    name: String,
    parked: Vec<ThreadId>,
    released: Vec<ThreadId>,
}

fn hooks() -> &'static (Mutex<Hooks>, Condvar) {
    static HOOKS: OnceLock<(Mutex<Hooks>, Condvar)> = OnceLock::new();
    HOOKS.get_or_init(|| (Mutex::new(Hooks::default()), Condvar::new()))
}
fn state() -> MutexGuard<'static, Hooks> {
    hooks().0.lock().unwrap_or_else(PoisonError::into_inner)
}
fn address<T>(lock: &Mutex<T>) -> usize {
    std::ptr::from_ref(lock).addr()
}

/// Called by [`super::lock`] before it blocks on `lock`.
pub(super) fn before_lock<T>(lock: &Mutex<T>) {
    let at = address(lock);
    let me = std::thread::current();
    let (_, changed) = hooks();
    let mut hooks = state();
    let gated = |hooks: &Hooks| {
        hooks.gates.iter().position(|g| {
            g.lock == at && me.name() == Some(g.name.as_str()) && !g.released.contains(&me.id())
        })
    };
    if let Some(index) = gated(&hooks) {
        hooks.gates[index].parked.push(me.id());
        changed.notify_all();
        while gated(&hooks).is_some() {
            hooks = changed.wait(hooks).unwrap_or_else(PoisonError::into_inner);
        }
    }
    let watched = hooks.blocked.iter().any(|(lock, _)| *lock == at);
    drop(hooks);
    if watched && matches!(lock.try_lock(), Err(TryLockError::WouldBlock)) {
        let mut hooks = state();
        if let Some((_, threads)) = hooks.blocked.iter_mut().find(|(lock, _)| *lock == at) {
            threads.push(me.id());
        }
        changed.notify_all();
    }
}

/// Waits on the hooks' condition until `done` holds or `patience` ran out; true when it held.
fn wait_until(patience: Duration, mut done: impl FnMut(&Hooks) -> bool) -> bool {
    let until = Instant::now() + patience;
    let (_, changed) = hooks();
    let mut hooks = state();
    while !done(&hooks) {
        let left = until.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return false;
        }
        hooks = changed
            .wait_timeout(hooks, left)
            .unwrap_or_else(PoisonError::into_inner)
            .0;
    }
    true
}

/// Records which threads find `lock` held while the guard lives.
pub(super) struct Blocked(usize);
impl Blocked {
    pub(super) fn watch<T>(lock: &Mutex<T>) -> Self {
        let at = address(lock);
        state().blocked.push((at, Vec::new()));
        Self(at)
    }
    /// True once `thread` found the lock held (it then waits for the holder), within `patience`.
    pub(super) fn wait_for(&self, thread: ThreadId, patience: Duration) -> bool {
        wait_until(patience, |hooks| {
            hooks
                .blocked
                .iter()
                .any(|(lock, threads)| *lock == self.0 && threads.contains(&thread))
        })
    }
}
impl Drop for Blocked {
    fn drop(&mut self) {
        state().blocked.retain(|(lock, _)| *lock != self.0);
    }
}

/// Parks every thread named `name` before it takes `lock`, until released; dropping the guard
/// releases them all.
pub(super) struct Gate(usize, String);
impl Gate {
    pub(super) fn close<T>(lock: &Mutex<T>, name: &str) -> Self {
        let at = address(lock);
        state().gates.push(GateState {
            lock: at,
            name: name.into(),
            parked: Vec::new(),
            released: Vec::new(),
        });
        Self(at, name.into())
    }
    /// The first `count` threads parked at this gate, in arrival order, within `patience`.
    pub(super) fn parked(&self, count: usize, patience: Duration) -> Option<Vec<ThreadId>> {
        let mine = |g: &GateState| g.lock == self.0 && g.name == self.1;
        if !wait_until(patience, |hooks| {
            hooks
                .gates
                .iter()
                .any(|g| mine(g) && g.parked.len() >= count)
        }) {
            return None;
        }
        state()
            .gates
            .iter()
            .find(|g| mine(g))
            .map(|g| g.parked[..count].to_vec())
    }
    /// Let `thread` go on (it is never parked here again).
    pub(super) fn release(&self, thread: ThreadId) {
        let mut hooks = state();
        if let Some(gate) = hooks
            .gates
            .iter_mut()
            .find(|g| g.lock == self.0 && g.name == self.1)
        {
            gate.released.push(thread);
        }
        hooks_changed();
    }
}
fn hooks_changed() {
    hooks().1.notify_all();
}
impl Drop for Gate {
    fn drop(&mut self) {
        state()
            .gates
            .retain(|g| !(g.lock == self.0 && g.name == self.1));
        hooks_changed();
    }
}
