// SPDX-License-Identifier: AGPL-3.0-only
//! Leaf-process lifecycle boundary for L6 composition. No activation/RPC format.
//! The executable must be a trusted non-forking leaf; this is not a sandbox or a
//! process-tree supervisor. Memory is reserved, not an OS RSS enforcement claim.
use crate::resources::{HeldReservation, ResourcePool};
use budgets::{Bytes, Reservation, Span};
use stage_contract::Error;
use std::{
    io,
    process::{Child, Command, ExitStatus, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, RecvTimeoutError, SyncSender, TrySendError},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

#[derive(Clone, Copy, Debug)]
pub struct ProcessLimits {
    /// Immutable monotonic deadline, translated by the authority-owning caller.
    pub deadline: Instant,
    /// Caller supplies typed registry/profile allowances; no local time defaults.
    pub close: Span,
    pub reap: Span,
    pub poll: Span,
}
fn duration(span: Span) -> Duration {
    Duration::from_millis(span.millis())
}
fn admission(limits: ProcessLimits, now: Instant) -> Result<(), ProcessError> {
    if now >= limits.deadline {
        return Err(ProcessError::Policy("worker-deadline-expired"));
    }
    if limits.close == Span::ZERO || limits.reap == Span::ZERO || limits.poll == Span::ZERO {
        return Err(ProcessError::Policy("zero-process-budget"));
    }
    if now.checked_add(duration(limits.close)).is_none()
        || now.checked_add(duration(limits.reap)).is_none()
    {
        return Err(ProcessError::Policy("process-budget-overflow"));
    }
    Ok(())
}
// The exact OS adapter and deterministic failures share one lifecycle core.
trait Leaf {
    fn id(&self) -> u32;
    fn try_wait(&mut self) -> io::Result<Option<ExitStatus>>;
    fn kill(&mut self) -> io::Result<()>;
    fn close_input(&mut self);
}
impl Leaf for Child {
    fn id(&self) -> u32 {
        Child::id(self)
    }
    fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        Child::try_wait(self)
    }
    fn kill(&mut self) -> io::Result<()> {
        Child::kill(self)
    }
    fn close_input(&mut self) {
        drop(self.stdin.take());
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ProcessError {
    Admission(Error),
    Policy(&'static str),
    Io {
        phase: &'static str,
        kind: io::ErrorKind,
    },
    MonitorPanic,
    MonitorDisconnected,
}
impl std::fmt::Display for ProcessError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for ProcessError {}
fn io_error(phase: &'static str, error: io::Error) -> ProcessError {
    ProcessError::Io {
        phase,
        kind: error.kind(),
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Terminal {
    Exited,
    Crashed,
    Cancelled,
    Deadline,
    Closed,
    CloseTimeout,
    SupervisorFailure,
}
#[derive(Clone, Debug)]
pub struct ProcessReport {
    pub pid: u32,
    pub terminal: Terminal,
    pub status: Option<ExitStatus>,
    /// Exact owned leaf reaped; no assertion about work success or disk cleanup.
    pub cleanup_proven: bool,
    pub errors: Vec<ProcessError>,
}
#[derive(Clone, Copy)]
enum Control {
    Cancel,
    Close,
}
struct ControlInbox {
    receiver: Receiver<Control>,
    cancelled: Arc<AtomicBool>,
}
fn control_channel() -> (SyncSender<Control>, ControlInbox) {
    let (sender, receiver) = mpsc::sync_channel(1);
    (
        sender,
        ControlInbox {
            receiver,
            cancelled: Arc::new(AtomicBool::new(false)),
        },
    )
}
impl ControlInbox {
    fn recv_timeout(&self, timeout: Duration) -> Result<Control, RecvTimeoutError> {
        if self.cancelled.load(Ordering::Acquire) {
            return Ok(Control::Cancel);
        }
        let notification = self.receiver.recv_timeout(timeout);
        if self.cancelled.load(Ordering::Acquire) {
            Ok(Control::Cancel)
        } else {
            notification
        }
    }
}

pub struct OwnedWorker {
    pid: u32,
    control: SyncSender<Control>,
    cancelled: Arc<AtomicBool>,
    monitor: Option<JoinHandle<ProcessReport>>,
    report: Option<ProcessReport>,
}
impl OwnedWorker {
    /// Reserve atomically before spawn, then supervise independently of callers.
    /// stdin is owned by this lifecycle: EOF requests graceful close. The caller
    /// owns the actual L5 data transport; stdout/stderr configuration is retained.
    pub fn spawn(
        mut command: Command,
        pool: ResourcePool,
        amount: Reservation,
        limits: ProcessLimits,
    ) -> Result<Self, ProcessError> {
        admission(limits, Instant::now())?;
        // Exit cannot prove staged-file deletion. L7 must retain that reservation.
        if amount.staged != Bytes::ZERO || amount.staging_overhead != Bytes::ZERO {
            return Err(ProcessError::Policy("process-exit-is-not-storage-cleanup"));
        }
        let (control, receiver) = control_channel();
        let cancelled = Arc::clone(&receiver.cancelled);
        let (started_tx, started_rx) = mpsc::sync_channel(1);
        command.stdin(Stdio::piped());
        // Last registration before spawn: exactly stdin/stdout/stderr reach the child (v0.1.4 used
        // `close_fds=True`). Fail closed; nothing is reserved yet, so a refusal leaves no residue.
        node::process::confine_descriptors(&mut command)
            .map_err(|error| io_error("confine-descriptors", error))?;
        let monitor = thread::Builder::new()
            .name("mlx-leaf-supervisor".into())
            .spawn(move || {
                let launch = (|| {
                    let mut held = pool.reserve(amount).map_err(ProcessError::Admission)?;
                    if let Err(error) = admission(limits, Instant::now()) {
                        held.release_verified().map_err(ProcessError::Admission)?;
                        return Err(error);
                    }
                    let spawned = command.spawn();
                    // v0.1.4 closes the parent's copy of every descriptor it handed the child right
                    // after `Popen` (`mycelium_node/process.py:2175-2177 @ f4e366e9`). A `Command`
                    // owns the caller's `Stdio::from(OwnedFd)`; keeping it until the leaf is reaped
                    // kept a parent-held stdio socket from ever seeing EOF
                    // (SE-SLICE1-L8-OWNEDWORKER-COMMAND-DROP). Dropped before `started` is sent, so
                    // the caller cannot observe a live parent copy once `spawn` returns.
                    drop(command);
                    match spawned {
                        Ok(child) => Ok((child, held)),
                        Err(error) => {
                            held.release_verified().map_err(ProcessError::Admission)?;
                            Err(io_error("spawn", error))
                        }
                    }
                })();
                match launch {
                    Ok((mut child, mut held)) => {
                        let pid = child.id();
                        if started_tx.send(Ok(pid)).is_err() {
                            return terminate(
                                &mut child,
                                &mut held,
                                Terminal::SupervisorFailure,
                                limits,
                                vec![ProcessError::MonitorDisconnected],
                            );
                        }
                        supervise_caught(&mut child, &mut held, receiver, limits)
                    }
                    Err(error) => {
                        let mut errors = vec![error.clone()];
                        if started_tx.send(Err(error)).is_err() {
                            errors.push(ProcessError::MonitorDisconnected);
                        }
                        ProcessReport {
                            pid: 0,
                            terminal: Terminal::SupervisorFailure,
                            status: None,
                            cleanup_proven: true,
                            errors,
                        }
                    }
                }
            })
            .map_err(|error| io_error("monitor-spawn", error))?;
        match started_rx.recv() {
            Ok(Ok(pid)) => Ok(Self {
                pid,
                control,
                cancelled,
                monitor: Some(monitor),
                report: None,
            }),
            Ok(Err(error)) => {
                monitor.join().map_err(|_| ProcessError::MonitorPanic)?;
                Err(error)
            }
            Err(_) => {
                monitor.join().map_err(|_| ProcessError::MonitorPanic)?;
                Err(ProcessError::MonitorDisconnected)
            }
        }
    }
    pub fn pid(&self) -> u32 {
        self.pid
    }
    pub fn cancel(&self) -> Result<(), ProcessError> {
        self.send(Control::Cancel)
    }
    pub fn request_close(&self) -> Result<(), ProcessError> {
        self.send(Control::Close)
    }
    fn send(&self, control: Control) -> Result<(), ProcessError> {
        if self.report.is_some() {
            return Ok(());
        }
        // A terminal monitor may have exited already; wait() still returns its
        // immutable result. A late cancel cannot rewrite natural completion.
        if matches!(control, Control::Cancel) {
            self.cancelled.store(true, Ordering::Release);
        }
        match self.control.try_send(control) {
            Ok(()) => Ok(()),
            // Coalesced wakeup: cancellation cannot be displaced by close.
            Err(TrySendError::Full(_)) => Ok(()),
            Err(TrySendError::Disconnected(_))
                if self
                    .monitor
                    .as_ref()
                    .is_some_and(|thread| thread.is_finished()) =>
            {
                Ok(())
            }
            Err(TrySendError::Disconnected(_)) => Err(ProcessError::MonitorDisconnected),
        }
    }
    pub fn wait(&mut self) -> Result<&ProcessReport, ProcessError> {
        if let Some(thread) = self.monitor.take() {
            self.report = Some(thread.join().map_err(|_| ProcessError::MonitorPanic)?);
        }
        self.report
            .as_ref()
            .ok_or(ProcessError::MonitorDisconnected)
    }
}
impl Drop for OwnedWorker {
    fn drop(&mut self) {
        if self.monitor.is_some() {
            if let Err(error) = self.cancel() {
                eprintln!("{error}; worker-drop cancel failed");
            }
            match self.wait() {
                Ok(report) if report.cleanup_proven && report.errors.is_empty() => (),
                Ok(report) => eprintln!(
                    "worker-drop cleanup={}; errors={:?}",
                    report.cleanup_proven, report.errors
                ),
                Err(error) => eprintln!("{error}; worker-drop cleanup unproven"),
            }
        }
    }
}
fn finish(
    child: &impl Leaf,
    held: &mut HeldReservation,
    status: ExitStatus,
    terminal: Terminal,
    mut errors: Vec<ProcessError>,
) -> ProcessReport {
    let cleanup_proven = match held.release_verified() {
        Ok(()) => true,
        Err(error) => {
            errors.push(ProcessError::Admission(error));
            false
        }
    };
    ProcessReport {
        pid: child.id(),
        terminal,
        status: Some(status),
        cleanup_proven,
        errors,
    }
}
fn supervise_caught(
    child: &mut impl Leaf,
    held: &mut HeldReservation,
    control: ControlInbox,
    limits: ProcessLimits,
) -> ProcessReport {
    // Catch only Rust supervisor unwinding; no native FFI runs in this process.
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        supervise(child, held, control, limits)
    })) {
        Ok(report) => report,
        Err(_) => terminate(
            child,
            held,
            Terminal::SupervisorFailure,
            limits,
            vec![ProcessError::MonitorPanic],
        ),
    }
}
fn supervise(
    child: &mut impl Leaf,
    held: &mut HeldReservation,
    control: ControlInbox,
    limits: ProcessLimits,
) -> ProcessReport {
    let mut close_deadline = None;
    loop {
        let now = Instant::now();
        if now >= limits.deadline {
            return terminate(child, held, Terminal::Deadline, limits, vec![]);
        }
        if close_deadline.is_some_and(|deadline| now >= deadline) {
            return terminate(child, held, Terminal::CloseTimeout, limits, vec![]);
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                let terminal = if !status.success() {
                    Terminal::Crashed
                } else if close_deadline.is_some() {
                    Terminal::Closed
                } else {
                    Terminal::Exited
                };
                return finish(child, held, status, terminal, vec![]);
            }
            Ok(None) => (),
            Err(error) => {
                return terminate(
                    child,
                    held,
                    Terminal::SupervisorFailure,
                    limits,
                    vec![io_error("poll", error)],
                );
            }
        }
        let next = close_deadline
            .unwrap_or(limits.deadline)
            .min(limits.deadline);
        match control.recv_timeout(duration(limits.poll).min(next.saturating_duration_since(now))) {
            Ok(Control::Cancel) | Err(RecvTimeoutError::Disconnected) => {
                return terminate(child, held, Terminal::Cancelled, limits, vec![]);
            }
            Ok(Control::Close) => {
                if close_deadline.is_none() {
                    // Repeated close never renews the original close window.
                    close_deadline = Some(
                        Instant::now()
                            .checked_add(duration(limits.close))
                            .unwrap_or(limits.deadline)
                            .min(limits.deadline),
                    );
                    child.close_input();
                }
            }
            // A timeout is the normal scheduled lifecycle/deadline poll.
            Err(RecvTimeoutError::Timeout) => continue,
        }
    }
}
fn terminate(
    child: &mut impl Leaf,
    held: &mut HeldReservation,
    terminal: Terminal,
    limits: ProcessLimits,
    mut errors: Vec<ProcessError>,
) -> ProcessReport {
    // std::process::Child retains ownership; never rediscover/signal a raw PID.
    match child.try_wait() {
        Ok(Some(status)) => {
            let terminal = if terminal == Terminal::Cancelled {
                if status.success() {
                    Terminal::Exited
                } else {
                    Terminal::Crashed
                }
            } else {
                terminal
            };
            return finish(child, held, status, terminal, errors);
        }
        Ok(None) => (),
        Err(error) => errors.push(io_error("pre-kill-poll", error)),
    }
    child.close_input();
    if let Err(error) = child.kill() {
        errors.push(io_error("kill", error));
    }
    let now = Instant::now();
    let deadline = now.checked_add(duration(limits.reap)).unwrap_or(now);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return finish(child, held, status, terminal, errors),
            Ok(None) => (),
            Err(error) => {
                errors.push(io_error("reap", error));
                break;
            }
        }
        let now = Instant::now();
        if now >= deadline {
            errors.push(ProcessError::Policy("reap-deadline"));
            break;
        }
        thread::sleep(duration(limits.poll).min(deadline.saturating_duration_since(now)));
    }
    // Dropping the unreleased token fences the pool; no successor can reuse it.
    ProcessReport {
        pid: child.id(),
        terminal,
        status: None,
        cleanup_proven: false,
        errors,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use budgets::BudgetId;
    fn limits() -> ProcessLimits {
        let now = Instant::now();
        ProcessLimits {
            deadline: now
                + duration(
                    BudgetId::WorkerStartup
                        .span(budgets::Entries::ZERO)
                        .unwrap(),
                ),
            close: BudgetId::RouterClose.span(budgets::Entries::ZERO).unwrap(),
            reap: BudgetId::WorkerCleanupReserve
                .span(budgets::Entries::ZERO)
                .unwrap(),
            poll: BudgetId::RegistryPoll.span(budgets::Entries::ZERO).unwrap(),
        }
    }
    fn reserve() -> (ResourcePool, HeldReservation) {
        let pool = ResourcePool::new(budgets::Resources {
            storage: Bytes::ZERO,
            memory: Bytes::new(10),
        });
        let held = pool
            .reserve(Reservation {
                staged: Bytes::ZERO,
                staging_overhead: Bytes::ZERO,
                resident: Bytes::new(10),
                scratch: Bytes::ZERO,
                kv: Bytes::ZERO,
            })
            .unwrap();
        (pool, held)
    }
    #[derive(Default)]
    struct FakeLeaf {
        polls: usize,
        kills: usize,
        closes: usize,
        fail_kill: bool,
        fail_poll: bool,
        panic_once: bool,
        exits_after_kill: bool,
    }
    #[cfg(unix)]
    impl Leaf for FakeLeaf {
        fn id(&self) -> u32 {
            42
        }
        fn close_input(&mut self) {
            self.closes += 1;
        }
        fn kill(&mut self) -> io::Result<()> {
            self.kills += 1;
            if self.fail_kill {
                Err(io::ErrorKind::PermissionDenied.into())
            } else {
                Ok(())
            }
        }
        fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
            use std::os::unix::process::ExitStatusExt;
            self.polls += 1;
            if self.panic_once {
                self.panic_once = false;
                panic!("injected monitor panic");
            }
            if self.fail_poll {
                return Err(io::ErrorKind::Other.into());
            }
            if self.kills > 0 && self.exits_after_kill {
                Ok(Some(ExitStatus::from_raw(9)))
            } else {
                Ok(None)
            }
        }
    }
    #[test]
    #[cfg(unix)]
    fn unproven_reap_retains_capacity_and_fences_successor() {
        let (pool, mut held) = reserve();
        let mut leaf = FakeLeaf {
            fail_kill: true,
            ..FakeLeaf::default()
        };
        let mut bounds = limits();
        bounds.reap = Span::ZERO;
        let report = terminate(&mut leaf, &mut held, Terminal::Cancelled, bounds, vec![]);
        assert!(!report.cleanup_proven);
        assert!(report.status.is_none());
        assert_eq!(
            report.errors,
            vec![
                ProcessError::Io {
                    phase: "kill",
                    kind: io::ErrorKind::PermissionDenied
                },
                ProcessError::Policy("reap-deadline")
            ]
        );
        assert_eq!((leaf.kills, leaf.closes), (1, 1));
        drop(held);
        assert_eq!(pool.used().unwrap().memory.get(), 10);
        assert_eq!(
            pool.healthy().unwrap_err().code,
            stage_contract::ErrorCode::Cleanup
        );
    }
    #[test]
    #[cfg(unix)]
    fn poll_failure_is_visible_and_cleanup_is_still_attempted() {
        let (_, mut held) = reserve();
        let mut leaf = FakeLeaf {
            fail_poll: true,
            ..FakeLeaf::default()
        };
        let (_, receiver) = control_channel();
        let report = supervise_caught(&mut leaf, &mut held, receiver, limits());
        assert_eq!(report.terminal, Terminal::SupervisorFailure);
        assert!(!report.cleanup_proven);
        assert_eq!(leaf.kills, 1);
        assert_eq!(
            report.errors,
            vec![
                ProcessError::Io {
                    phase: "poll",
                    kind: io::ErrorKind::Other
                },
                ProcessError::Io {
                    phase: "pre-kill-poll",
                    kind: io::ErrorKind::Other
                },
                ProcessError::Io {
                    phase: "reap",
                    kind: io::ErrorKind::Other
                },
            ]
        );
    }
    #[test]
    #[cfg(unix)]
    fn monitor_panic_kills_and_reaps_before_releasing_capacity() {
        let (pool, mut held) = reserve();
        let mut leaf = FakeLeaf {
            panic_once: true,
            exits_after_kill: true,
            ..FakeLeaf::default()
        };
        let (_, receiver) = control_channel();
        let report = supervise_caught(&mut leaf, &mut held, receiver, limits());
        assert_eq!(report.terminal, Terminal::SupervisorFailure);
        assert_eq!(report.errors, vec![ProcessError::MonitorPanic]);
        assert!(report.cleanup_proven);
        assert_eq!(leaf.kills, 1);
        assert_eq!(pool.used().unwrap(), budgets::Resources::default());
        pool.healthy().unwrap();
    }
    #[test]
    fn lifecycle_control_notifications_are_bounded() {
        let (control, receiver) = control_channel();
        let worker = OwnedWorker {
            pid: 0,
            control,
            cancelled: Arc::clone(&receiver.cancelled),
            monitor: None,
            report: None,
        };
        for _ in 0..1024 {
            worker.request_close().unwrap();
        }
        worker.cancel().unwrap();
        assert!(
            matches!(receiver.recv_timeout(Duration::ZERO), Ok(Control::Cancel)),
            "cancel must outrank queued close"
        );
        assert_eq!(
            receiver.receiver.try_iter().count(),
            1,
            "control must coalesce, not grow an unbounded mailbox"
        );
    }
    #[test]
    fn expired_worker_deadline_refuses_before_launch() {
        let now = Instant::now();
        let limits = ProcessLimits {
            deadline: now,
            close: BudgetId::RouterClose.span(budgets::Entries::ZERO).unwrap(),
            reap: BudgetId::WorkerCleanupReserve
                .span(budgets::Entries::ZERO)
                .unwrap(),
            poll: BudgetId::RegistryPoll.span(budgets::Entries::ZERO).unwrap(),
        };
        assert_eq!(
            admission(limits, now),
            Err(ProcessError::Policy("worker-deadline-expired"))
        );
    }
}
