// SPDX-License-Identifier: AGPL-3.0-only
//! Lane ST patch round 1 (review ST-01): a probe line must never block, panic, poison a lock or
//! grow without bound, whatever its sink does.
use super::*;
use std::{
    io,
    panic::{AssertUnwindSafe, catch_unwind},
    process::Command,
    sync::{Arc, Condvar, mpsc},
    thread,
    time::Duration,
};

/// How long a step that must be prompt may take (generous: a loaded build host).
const PROMPT: Duration = Duration::from_secs(5);

/// A sink stuck in its write (a stderr pipe nobody drains) until the test opens it.
struct Gated {
    entered: mpsc::Sender<()>,
    shared: Arc<GateState>,
}
struct GateState {
    open: Mutex<bool>,
    opened: Condvar,
    lines: Mutex<Vec<String>>,
}
impl Sink for Gated {
    fn write_line(&mut self, line: &[u8]) -> io::Result<()> {
        self.entered.send(()).unwrap_or(());
        let mut open = self
            .shared
            .open
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        while !*open {
            open = self
                .shared
                .opened
                .wait(open)
                .unwrap_or_else(PoisonError::into_inner);
        }
        drop(open);
        self.shared
            .lines
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(String::from_utf8_lossy(line).into_owned());
        Ok(())
    }
}
fn gated() -> (Gated, mpsc::Receiver<()>, Arc<GateState>) {
    let (entered, entered_rx) = mpsc::channel();
    let shared = Arc::new(GateState {
        open: Mutex::new(false),
        opened: Condvar::new(),
        lines: Mutex::new(Vec::new()),
    });
    let sink = Gated {
        entered,
        shared: shared.clone(),
    };
    (sink, entered_rx, shared)
}
impl GateState {
    fn open(&self) {
        *self.open.lock().unwrap_or_else(PoisonError::into_inner) = true;
        self.opened.notify_all();
    }
    fn lines(&self) -> Vec<String> {
        self.lines
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
}

/// A sink whose every write fails (EIO / ENOSPC on the log file).
struct Failing;
impl Sink for Failing {
    fn write_line(&mut self, _line: &[u8]) -> io::Result<()> {
        Err(io::Error::other("probe sink failed"))
    }
}

fn wait_until(what: &str, done: impl Fn() -> bool) {
    let started = Instant::now();
    while !done() {
        assert!(started.elapsed() < PROMPT, "timed out waiting for {what}");
        thread::sleep(Duration::from_millis(5));
    }
}

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

/// ST-01 acceptance (1): the sink is stuck and the queue is full while a slow probed guard is
/// released; the next waiter still takes the lock at once.
#[test]
fn a_stuck_sink_never_keeps_a_waiter_from_a_released_lock() {
    let (sink, entered, gate) = gated();
    let recorder = Recorder::with_sink(1, Box::new(sink));
    let mutex = Mutex::new(0u32);
    let (recorder, mutex) = (&recorder, &mutex);
    let (held, acquired) = thread::scope(|s| {
        // The sink takes one line and never returns from writing it.
        s.spawn(move || recorder.record(format_args!("l11_first")));
        let stuck = entered.recv_timeout(PROMPT).is_ok();
        // Every wait and hold is reported (threshold 0): the slow holder's lines.
        let (held_tx, held_rx) = mpsc::channel();
        s.spawn(move || {
            *lock_in(mutex, recorder, 0).unwrap_or_else(PoisonError::into_inner) += 1;
            held_tx.send(()).unwrap_or(());
        });
        let held = held_rx.recv_timeout(PROMPT).is_ok();
        let (taken_tx, taken_rx) = mpsc::channel();
        s.spawn(move || {
            let value = *lock_in(mutex, recorder, 0).unwrap_or_else(PoisonError::into_inner);
            taken_tx.send(value).unwrap_or(());
        });
        let acquired = taken_rx.recv_timeout(PROMPT);
        gate.open();
        assert!(stuck, "the sink never took the first line");
        (held, acquired)
    });
    assert!(held, "the holder's own release waited for the stuck sink");
    assert_eq!(acquired, Ok(1), "a waiter waited for the stuck sink");
    assert!(!mutex.is_poisoned());
}

/// ST-01 acceptance (2): a failing write neither panics the caller nor poisons its lock, and is
/// counted.
#[test]
fn a_failing_sink_neither_panics_nor_poisons_and_is_counted() {
    let recorder = Recorder::with_sink(4, Box::new(Failing));
    let mutex = Mutex::new(0u32);
    let ran = catch_unwind(AssertUnwindSafe(|| {
        *lock_in(&mutex, &recorder, 0).unwrap_or_else(PoisonError::into_inner) += 1;
    }));
    assert!(ran.is_ok(), "a diagnostic write error panicked the caller");
    assert!(
        !mutex.is_poisoned(),
        "a diagnostic write error poisoned the lock"
    );
    assert!(lock(&mutex).is_ok());
    // the wait line and the hold line both failed in the sink
    wait_until("two counted write errors", || recorder.write_errors() >= 2);
    assert_eq!(recorder.dropped(), 0);
}

const UNWIND_CHILD: &str = "MYCELIUM_SEED_PROBE_UNWIND_CHILD";
const UNWIND_TEST: &str = "probe::tests::a_hold_reported_during_an_unwind_does_not_abort";

/// ST-01 acceptance (3): a hold reported while the holder unwinds (its own panic) over a failing
/// sink does not panic again: no abort, and the caller's panic is the one that arrives. Run in a
/// child process, since a double panic aborts the whole test binary.
#[test]
fn a_hold_reported_during_an_unwind_does_not_abort() {
    if std::env::var_os(UNWIND_CHILD).is_some() {
        let recorder = Recorder::with_sink(4, Box::new(Failing));
        let mutex = Mutex::new(0u32);
        // The wait (uncontended) stays below the threshold; the hold crosses it.
        let ran = catch_unwind(AssertUnwindSafe(|| {
            let _held = lock_in(&mutex, &recorder, 300);
            thread::sleep(Duration::from_millis(600));
            panic!("the caller's own panic");
        }));
        let message = ran
            .err()
            .and_then(|payload| payload.downcast_ref::<&str>().map(|s| (*s).to_owned()));
        println!("unwind_child message={message:?}");
        wait_until("the hold line's counted write error", || {
            recorder.write_errors() >= 1
        });
        println!("unwind_child write_errors={}", recorder.write_errors());
        return;
    }
    let exe = std::env::current_exe().expect("test executable");
    let out = Command::new(exe)
        .args([UNWIND_TEST, "--exact", "--nocapture", "--test-threads", "1"])
        .env(UNWIND_CHILD, "1")
        .output()
        .expect("child test process");
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(
        out.status.success(),
        "the child aborted or failed: {:?}\nstdout:\n{stdout}\nstderr:\n{}",
        out.status,
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        stdout.contains("unwind_child message=Some(\"the caller's own panic\")"),
        "{stdout}"
    );
    assert!(stdout.contains("unwind_child write_errors=1"), "{stdout}");
}

/// A full queue refuses the line at once and counts it; the next line drained reports the count.
#[test]
fn a_full_queue_drops_and_counts_and_the_next_line_reports_it() {
    let (sink, entered, gate) = gated();
    let recorder = Recorder::with_sink(1, Box::new(sink));
    let recorder = &recorder;
    let done = thread::scope(|s| {
        let (done_tx, done_rx) = mpsc::channel();
        s.spawn(move || {
            recorder.record(format_args!("l11_a"));
            let stuck = entered.recv_timeout(PROMPT).is_ok();
            recorder.record(format_args!("l11_b"));
            recorder.record(format_args!("l11_c"));
            recorder.record(format_args!("l11_d"));
            done_tx.send(stuck).unwrap_or(());
        });
        let done = done_rx.recv_timeout(PROMPT);
        gate.open();
        done
    });
    assert_eq!(done, Ok(true), "recording waited for the stuck sink");
    assert_eq!(recorder.dropped(), 2, "c and d found the queue full");
    wait_until("three lines written", || gate.lines().len() >= 3);
    let lines = gate.lines();
    assert_eq!(lines.first().map(String::as_str), Some("l11_a\n"));
    let lost = lines.get(1).map(String::as_str).unwrap_or("");
    assert!(
        lost.starts_with("l11_probe_lost dropped_total=2 write_errors_total=0 wall_ms="),
        "{lines:?}"
    );
    assert_eq!(lines.get(2).map(String::as_str), Some("l11_b\n"));
    assert_eq!(lines.len(), 3, "{lines:?}");
}

/// A caller-supplied field (a relay path) cannot grow a line past [`LINE_BYTES`].
#[test]
fn a_long_line_is_cut_not_grown() {
    let (sink, _entered, gate) = gated();
    gate.open();
    let recorder = Recorder::with_sink(4, Box::new(sink));
    let path = "/seed/".to_owned() + &"x".repeat(10_000);
    recorder.record(format_args!(
        "l11_relay_backend_unavailable path={path} stage=hop"
    ));
    wait_until("the line written", || recorder.written() >= 1);
    let lines = gate.lines();
    let line = lines.first().map(String::as_str).unwrap_or("");
    assert_eq!(line.len(), LINE_BYTES, "{}", line.len());
    assert!(line.starts_with("l11_relay_backend_unavailable path=/seed/xxx"));
    assert!(line.ends_with("...\n"), "a cut line says so");
}
