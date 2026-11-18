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
//! Every probe line (these and the relay's) goes through a [`Recorder`], which never blocks, never
//! panics and never writes on the caller's thread (review ST-01: `eprintln!` takes stderr's global
//! lock and panics on a write error, so a stalled or failing log sink could extend a lock hold
//! without bound, poison the lock, abort an unwinding thread, or end the relay's accept thread).
//! A line is formatted into a fixed [`LINE_BYTES`] buffer (cut, never grown) and offered to a
//! bounded queue of [`QUEUE_LINES`]; one lazily started daemon thread drains it to stderr. A full
//! queue drops the line and counts it; a failed write is counted; the next line drained is preceded
//! by `l11_probe_lost dropped_total=<n> write_errors_total=<n> wall_ms=<ms>` whenever either total
//! moved. Lines still queued when the process exits are lost (log-only).
use std::{
    fmt::{self, Write as _},
    io::Write as _,
    ops::{Deref, DerefMut},
    panic::Location,
    sync::{
        Arc, Mutex, MutexGuard, OnceLock, PoisonError,
        atomic::{AtomicU64, Ordering},
        mpsc::{Receiver, SyncSender, sync_channel},
    },
    thread,
    time::{Instant, SystemTime, UNIX_EPOCH},
};

/// A wait or hold this long (milliseconds) is reported. Log threshold only; it bounds nothing.
pub const REPORT_MS: u128 = 1000;
/// The longest probe line, in bytes, newline included. 512, not 256 (lane SR round 3, review
/// SR2-02): a hello bootstrap refusal names every mismatching binding field after the line's
/// node / run / offer, and at the longest run id the protocol admits (103 characters) the cut
/// fell inside the field list. Still a fixed buffer, still cut, never grown.
pub const LINE_BYTES: usize = 512;
/// Probe lines waiting for the drain thread; one more is dropped and counted.
pub const QUEUE_LINES: usize = 256;

/// Wall-clock milliseconds since the Unix epoch, as the `SLICE_PHASE` lines carry them (`0` when
/// the clock is before the epoch).
pub fn wall_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis())
}

/// Where probe lines go: stderr in production, a test's own sink through [`Recorder::with_sink`].
/// Only the recorder's drain thread calls it.
pub trait Sink: Send + 'static {
    /// Write one whole line, newline included.
    fn write_line(&mut self, line: &[u8]) -> std::io::Result<()>;
}
/// The process's stderr, written through `Stderr::write_all` (never `eprintln!`, which panics on a
/// write error).
struct Stderr;
impl Sink for Stderr {
    fn write_line(&mut self, line: &[u8]) -> std::io::Result<()> {
        std::io::stderr().lock().write_all(line)
    }
}

/// One probe line in a fixed buffer: formatting never allocates, and a line too long is cut and
/// ends in `...`.
struct Line {
    buf: [u8; LINE_BYTES],
    len: usize,
    cut: bool,
}
impl Line {
    fn format(args: fmt::Arguments<'_>) -> Line {
        let mut line = Line {
            buf: [0; LINE_BYTES],
            len: 0,
            cut: false,
        };
        // `Line::write_str` never fails; a `Display` that does just ends the line early.
        line.write_fmt(args).unwrap_or(());
        if line.cut {
            line.len = line.len.saturating_sub(3);
            line.push(b"...");
        }
        // the content stops one byte short of the buffer, so the newline always fits
        line.len = line.len.min(LINE_BYTES.saturating_sub(1));
        line.push(b"\n");
        line
    }
    /// Append what fits (the newline's byte kept free unless this is the newline).
    fn push(&mut self, bytes: &[u8]) {
        for (slot, byte) in self.buf.iter_mut().skip(self.len).zip(bytes) {
            *slot = *byte;
            self.len = self.len.saturating_add(1);
        }
    }
    fn bytes(&self) -> &[u8] {
        self.buf.get(..self.len).unwrap_or(&[])
    }
}
impl fmt::Write for Line {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        let room = LINE_BYTES.saturating_sub(1).saturating_sub(self.len);
        if s.len() > room {
            self.cut = true;
        }
        let take = s.len().min(room);
        self.push(s.as_bytes().get(..take).unwrap_or(&[]));
        Ok(())
    }
}

/// The recorder's counters, shared with its drain thread.
#[derive(Default)]
struct Counts {
    dropped: AtomicU64,
    write_errors: AtomicU64,
    written: AtomicU64,
}

/// Probe lines' bounded, non-blocking recorder (see the module documentation).
pub struct Recorder {
    capacity: usize,
    /// The sink until the first line moves it into the drain thread.
    sink: Mutex<Option<Box<dyn Sink>>>,
    /// Set at the first line; `None` when the drain thread could not be started (every line is then
    /// dropped and counted).
    queue: OnceLock<Option<SyncSender<Line>>>,
    counts: Arc<Counts>,
}
impl Recorder {
    /// The process's recorder, over stderr.
    pub fn global() -> &'static Recorder {
        static GLOBAL: OnceLock<Recorder> = OnceLock::new();
        GLOBAL.get_or_init(|| Recorder::with_sink(QUEUE_LINES, Box::new(Stderr)))
    }
    /// A recorder of `capacity` queued lines (at least one) over `sink` (test seam). Its drain
    /// thread starts at the first line.
    pub fn with_sink(capacity: usize, sink: Box<dyn Sink>) -> Recorder {
        Recorder {
            capacity: capacity.max(1),
            sink: Mutex::new(Some(sink)),
            queue: OnceLock::new(),
            counts: Arc::new(Counts::default()),
        }
    }
    /// The queue, starting the drain thread on first use (a bounded spawn, once).
    fn queue(&self) -> Option<&SyncSender<Line>> {
        self.queue
            .get_or_init(|| {
                let sink = self
                    .sink
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner)
                    .take()?;
                let (queue, lines) = sync_channel(self.capacity);
                let counts = self.counts.clone();
                thread::Builder::new()
                    .name("l11-probe-drain".into())
                    .spawn(move || drain_lines(&lines, sink, &counts))
                    .is_ok()
                    .then_some(queue)
            })
            .as_ref()
    }
    /// Offer one line to the queue: never blocks, never panics, never writes on this thread. A full
    /// queue (or no drain thread) drops the line and counts it.
    pub fn record(&self, args: fmt::Arguments<'_>) {
        let line = Line::format(args);
        let queued = self
            .queue()
            .is_some_and(|queue| queue.try_send(line).is_ok());
        if !queued {
            self.counts.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }
    /// Lines dropped because the queue was full (or there was no drain thread).
    pub fn dropped(&self) -> u64 {
        self.counts.dropped.load(Ordering::Relaxed)
    }
    /// Lines the sink failed to write.
    pub fn write_errors(&self) -> u64 {
        self.counts.write_errors.load(Ordering::Relaxed)
    }
    /// Lines the sink accepted (`l11_probe_lost` lines included).
    pub fn written(&self) -> u64 {
        self.counts.written.load(Ordering::Relaxed)
    }
}
/// The drain thread: write each queued line, preceded by the lost totals whenever they moved.
/// Ends when its recorder is dropped.
fn drain_lines(lines: &Receiver<Line>, mut sink: Box<dyn Sink>, counts: &Counts) {
    let write = |sink: &mut dyn Sink, line: &Line| {
        let counter = match sink.write_line(line.bytes()) {
            Ok(()) => &counts.written,
            Err(_) => &counts.write_errors,
        };
        counter.fetch_add(1, Ordering::Relaxed);
    };
    let mut reported = (0, 0);
    while let Ok(line) = lines.recv() {
        let lost = (
            counts.dropped.load(Ordering::Relaxed),
            counts.write_errors.load(Ordering::Relaxed),
        );
        if lost != reported {
            reported = lost;
            let (dropped, write_errors) = lost;
            let note = Line::format(format_args!(
                "l11_probe_lost dropped_total={dropped} write_errors_total={write_errors} wall_ms={}",
                wall_ms()
            ));
            write(sink.as_mut(), &note);
        }
        write(sink.as_mut(), &line);
    }
}

/// Record one probe line on the process's recorder ([`Recorder::record`]).
pub fn record(args: fmt::Arguments<'_>) {
    Recorder::global().record(args);
}

/// A held lock that reports its own hold when it is released.
///
/// Field order matters: `guard` is dropped first, so the mutex is released before `_hold` records
/// the hold line (its only use is its `Drop`).
pub struct Probed<'a, T> {
    guard: MutexGuard<'a, T>,
    _hold: Hold<'a>,
}
/// The hold line of one [`Probed`], recorded after the mutex is released.
struct Hold<'a> {
    recorder: &'a Recorder,
    report_ms: u128,
    lock: &'static str,
    site: &'static Location<'static>,
    /// Taken right after the lock was acquired, before any line is recorded.
    since: Instant,
}
impl Drop for Hold<'_> {
    fn drop(&mut self) {
        let held = self.since.elapsed().as_millis();
        if held >= self.report_ms {
            self.recorder.record(format_args!(
                "l11_lock_hold lock={} site={}:{} held_ms={held} wall_ms={}",
                self.lock,
                self.site.file(),
                self.site.line(),
                wall_ms()
            ));
        }
    }
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
    let since = Instant::now();
    let lock = std::any::type_name::<T>();
    let waited = since.saturating_duration_since(asked).as_millis();
    if waited >= report_ms {
        recorder.record(format_args!(
            "l11_lock_wait lock={lock} site={}:{} waited_ms={waited} wall_ms={}",
            site.file(),
            site.line(),
            wall_ms()
        ));
    }
    let hold = Hold {
        recorder,
        report_ms,
        lock,
        site,
        since,
    };
    match taken {
        Ok(guard) => Ok(Probed { guard, _hold: hold }),
        Err(poisoned) => Err(PoisonError::new(Probed {
            guard: poisoned.into_inner(),
            _hold: hold,
        })),
    }
}

/// `mutex.lock().unwrap_or_else(PoisonError::into_inner)`, probed.
#[track_caller]
pub fn lock_or_poisoned<T>(mutex: &Mutex<T>) -> Probed<'_, T> {
    lock(mutex).unwrap_or_else(PoisonError::into_inner)
}

#[cfg(test)]
mod tests;
