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
//!
//! Every probe line (these and the relay's) goes through a [`Recorder`]; [`Recorder::global`] is
//! stderr (`eprintln!`), and a test injects its own [`Sink`].
use std::{
    fmt,
    ops::{Deref, DerefMut},
    panic::Location,
    sync::{
        Mutex, MutexGuard, PoisonError,
        atomic::{AtomicU64, Ordering},
    },
    time::{Instant, SystemTime, UNIX_EPOCH},
};

/// A wait or hold this long (milliseconds) is reported. Log threshold only; it bounds nothing.
pub const REPORT_MS: u128 = 1000;
/// The longest probe line, in bytes, newline included.
pub const LINE_BYTES: usize = 256;

/// Wall-clock milliseconds since the Unix epoch, as the `SLICE_PHASE` lines carry them (`0` when
/// the clock is before the epoch).
pub fn wall_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis())
}

/// Where probe lines go (a test seam; production is stderr).
pub trait Sink: Send + 'static {
    /// Write one whole line, newline included.
    fn write_line(&mut self, line: &[u8]) -> std::io::Result<()>;
}

/// The probe lines' recorder: `eprintln!` semantics, written by the caller.
pub struct Recorder {
    sink: Option<Mutex<Box<dyn Sink>>>,
    written: AtomicU64,
}
static GLOBAL: Recorder = Recorder {
    sink: None,
    written: AtomicU64::new(0),
};
impl Recorder {
    /// The process's recorder: stderr.
    pub fn global() -> &'static Recorder {
        &GLOBAL
    }
    /// A recorder over `sink` (test seam).
    pub fn with_sink(_capacity: usize, sink: Box<dyn Sink>) -> Recorder {
        Recorder {
            sink: Some(Mutex::new(sink)),
            written: AtomicU64::new(0),
        }
    }
    /// Record one line, as `eprintln!` does: the caller writes it, and a write error panics.
    pub fn record(&self, args: fmt::Arguments<'_>) {
        match &self.sink {
            None => eprintln!("{args}"),
            Some(sink) => {
                let line = format!("{args}\n");
                let mut sink = sink.lock().unwrap_or_else(PoisonError::into_inner);
                if let Err(error) = sink.write_line(line.as_bytes()) {
                    panic!("failed printing to stderr: {error}");
                }
            }
        }
        self.written.fetch_add(1, Ordering::Relaxed);
    }
    /// Lines refused because the queue was full.
    pub fn dropped(&self) -> u64 {
        0
    }
    /// Lines the sink failed to write.
    pub fn write_errors(&self) -> u64 {
        0
    }
    /// Lines the sink accepted.
    pub fn written(&self) -> u64 {
        self.written.load(Ordering::Relaxed)
    }
}

/// Record one probe line on the process's recorder.
pub fn record(args: fmt::Arguments<'_>) {
    Recorder::global().record(args);
}

/// A held lock that reports its own hold when it is released.
pub struct Probed<'a, T> {
    guard: MutexGuard<'a, T>,
    recorder: &'a Recorder,
    report_ms: u128,
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
        if held >= self.report_ms {
            self.recorder.record(format_args!(
                "l11_lock_hold lock={} site={}:{} held_ms={held} wall_ms={}",
                std::any::type_name::<T>(),
                self.site.file(),
                self.site.line(),
                wall_ms()
            ));
        }
    }
}

/// `mutex.lock()`, probed: `Err` exactly when the lock is poisoned, carrying the probed guard.
#[track_caller]
pub fn lock<T>(mutex: &Mutex<T>) -> Result<Probed<'_, T>, PoisonError<Probed<'_, T>>> {
    lock_in(mutex, Recorder::global(), REPORT_MS)
}

/// [`lock`] over a given recorder and threshold (test seam).
#[track_caller]
pub fn lock_in<'a, T>(
    mutex: &'a Mutex<T>,
    recorder: &'a Recorder,
    report_ms: u128,
) -> Result<Probed<'a, T>, PoisonError<Probed<'a, T>>> {
    let site = Location::caller();
    let asked = Instant::now();
    let taken = mutex.lock();
    let waited = asked.elapsed().as_millis();
    if waited >= report_ms {
        recorder.record(format_args!(
            "l11_lock_wait lock={} site={}:{} waited_ms={waited} wall_ms={}",
            std::any::type_name::<T>(),
            site.file(),
            site.line(),
            wall_ms()
        ));
    }
    let since = Instant::now();
    let probed = |guard| Probed {
        guard,
        recorder,
        report_ms,
        site,
        since,
    };
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
mod tests;
