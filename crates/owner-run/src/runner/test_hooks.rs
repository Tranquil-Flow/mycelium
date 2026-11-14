// SPDX-License-Identifier: AGPL-3.0-only
//! Test-only observation of the Runner's own lock helper ([`super::lock`]), so schedule tests
//! are decided by facts, never by timing:
//! - [`Blocked`]: which threads found a watched lock HELD when they came to take it. A reader
//!   recorded there cannot have read before the holder released the lock, so a test can prove a
//!   status read or a Stop is waiting behind a parked commit before it releases the commit.
//! - [`Gate`]: parks every thread with a given name each time it comes to take a given lock,
//!   until the test lets it pass once ([`Gate::pass`]) or for good ([`Gate::free`]) (the
//!   rebuild watcher, which takes the run lock to check and again to expire).
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
    /// Every arrival at the gate, in order (a thread that comes back arrives again).
    arrivals: Vec<ThreadId>,
    /// One pass per entry.
    passes: Vec<ThreadId>,
    freed: Vec<ThreadId>,
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

/// The gate `me` is held at before taking the lock at `at`, unless it was freed there.
fn gate_of<'h>(
    hooks: &'h mut Hooks,
    at: usize,
    me: &std::thread::Thread,
) -> Option<&'h mut GateState> {
    hooks
        .gates
        .iter_mut()
        .find(|g| g.lock == at && me.name() == Some(g.name.as_str()) && !g.freed.contains(&me.id()))
}

/// Called by [`super::lock`] before it blocks on `lock`.
pub(super) fn before_lock<T>(lock: &Mutex<T>) {
    let at = address(lock);
    let me = std::thread::current();
    let (_, changed) = hooks();
    let mut hooks = state();
    if let Some(gate) = gate_of(&mut hooks, at, &me) {
        gate.arrivals.push(me.id());
        changed.notify_all();
        loop {
            // Gone (dropped) or freed: go on. A pass is used up by this one acquisition.
            let Some(gate) = gate_of(&mut hooks, at, &me) else {
                break;
            };
            if let Some(at) = gate.passes.iter().position(|t| *t == me.id()) {
                gate.passes.remove(at);
                break;
            }
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

/// Parks every thread named `name` each time it comes to take `lock`; dropping the guard lets
/// them all go.
pub(super) struct Gate(usize, String);
impl Gate {
    pub(super) fn close<T>(lock: &Mutex<T>, name: &str) -> Self {
        let at = address(lock);
        state().gates.push(GateState {
            lock: at,
            name: name.into(),
            arrivals: Vec::new(),
            passes: Vec::new(),
            freed: Vec::new(),
        });
        Self(at, name.into())
    }
    /// The first `count` arrivals at this gate, in order, once there are that many (within
    /// `patience`).
    pub(super) fn arrivals(&self, count: usize, patience: Duration) -> Option<Vec<ThreadId>> {
        let mine = |g: &GateState| g.lock == self.0 && g.name == self.1;
        if !wait_until(patience, |hooks| {
            hooks
                .gates
                .iter()
                .any(|g| mine(g) && g.arrivals.len() >= count)
        }) {
            return None;
        }
        state()
            .gates
            .iter()
            .find(|g| mine(g))
            .map(|g| g.arrivals[..count].to_vec())
    }
    fn update(&self, f: impl FnOnce(&mut GateState)) {
        let mut hooks = state();
        if let Some(gate) = hooks
            .gates
            .iter_mut()
            .find(|g| g.lock == self.0 && g.name == self.1)
        {
            f(gate);
        }
        drop(hooks);
        hooks_changed();
    }
    /// Let `thread` take the lock once; it is parked again when it comes back.
    pub(super) fn pass(&self, thread: ThreadId) {
        self.update(|gate| gate.passes.push(thread));
    }
    /// Let `thread` go on for good.
    pub(super) fn free(&self, thread: ThreadId) {
        self.update(|gate| gate.freed.push(thread));
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
