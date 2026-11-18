// SPDX-License-Identifier: AGPL-3.0-only
//! Log-only lock probes (RV42 S1 relay-502 RCA, `analysis/RV42-S1-RELAY-502-RCA.md`): a joined
//! Mac's relayed requests and the owner's page status stalled together for 5-131 s, and no log line
//! named the thread holding the shared coordinator / authority section. A probed lock prints one
//! stderr line when a caller WAITED for it, or HELD it, for [`REPORT_MS`] or longer, with the call
//! site (`#[track_caller]`) and the lock's type:
//!
//! ```text
//! l11_lock_wait lock=<type> site=<file>:<line> waited_ms=<n> wall_ms=<unix ms>
//! l11_lock_hold lock=<type> site=<file>:<line> held_ms=<n> wall_ms=<unix ms>
//! ```
//!
//! A hold line names the holder that made the waiters wait. Observation only: the lock taken, its
//! poison handling and every caller's behaviour are unchanged; nothing is logged below the threshold.
use std::{
    ops::{Deref, DerefMut},
    panic::Location,
    sync::{Mutex, MutexGuard, PoisonError},
    time::{Instant, SystemTime, UNIX_EPOCH},
};

/// A wait or hold this long (milliseconds) is reported. Log threshold only; it bounds nothing.
pub const REPORT_MS: u128 = 1000;

/// Wall-clock milliseconds since the Unix epoch, as the `SLICE_PHASE` lines carry them (`0` when
/// the clock is before the epoch).
pub fn wall_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis())
}

/// A held lock that reports its own hold when it is released.
pub struct Probed<'a, T> {
    guard: MutexGuard<'a, T>,
    site: &'static Location<'static>,
    since: Instant,
}
impl<T> Deref for Probed<'_, T> {
    type Target = T;
    fn deref(&self) -> &T {
        &self.guard
    }
}
impl<T> DerefMut for Probed<'_, T> {
    fn deref_mut(&mut self) -> &mut T {
        &mut self.guard
    }
}
impl<T> Drop for Probed<'_, T> {
    fn drop(&mut self) {
        let held = self.since.elapsed().as_millis();
        if held >= REPORT_MS {
            eprintln!(
                "l11_lock_hold lock={} site={}:{} held_ms={held} wall_ms={}",
                std::any::type_name::<T>(),
                self.site.file(),
                self.site.line(),
                wall_ms()
            );
        }
    }
}

/// `mutex.lock()`, probed: `Err` exactly when the lock is poisoned, carrying the probed guard.
#[track_caller]
pub fn lock<T>(mutex: &Mutex<T>) -> Result<Probed<'_, T>, PoisonError<Probed<'_, T>>> {
    let site = Location::caller();
    let asked = Instant::now();
    let taken = mutex.lock();
    let waited = asked.elapsed().as_millis();
    if waited >= REPORT_MS {
        eprintln!(
            "l11_lock_wait lock={} site={}:{} waited_ms={waited} wall_ms={}",
            std::any::type_name::<T>(),
            site.file(),
            site.line(),
            wall_ms()
        );
    }
    let since = Instant::now();
    let probed = |guard| Probed { guard, site, since };
    match taken {
        Ok(guard) => Ok(probed(guard)),
        Err(poisoned) => Err(PoisonError::new(probed(poisoned.into_inner()))),
    }
}

/// `mutex.lock().unwrap_or_else(PoisonError::into_inner)`, probed.
#[track_caller]
pub fn lock_or_poisoned<T>(mutex: &Mutex<T>) -> Probed<'_, T> {
    lock(mutex).unwrap_or_else(PoisonError::into_inner)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_probed_lock_is_the_same_lock_and_keeps_its_poison() {
        let mutex = Mutex::new(1u32);
        *lock_or_poisoned(&mutex) += 1;
        assert_eq!(*mutex.lock().unwrap(), 2);
        assert!(lock(&mutex).is_ok());
        let poisoned = std::panic::catch_unwind(|| {
            let _held = mutex.lock().unwrap();
            panic!("poison it");
        });
        assert!(poisoned.is_err());
        assert!(lock(&mutex).is_err(), "poison is reported as before");
        assert_eq!(*lock_or_poisoned(&mutex), 2, "and still yields the value");
    }
}
