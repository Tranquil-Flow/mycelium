// SPDX-License-Identifier: AGPL-3.0-only
// P:path:mycelium_node/process.py (Python `subprocess` close_fds=True: a child never kept
// a parent socket). Real children only; no model, app or fixed port. Every test forces
// descriptors into the socket()/FD_CLOEXEC gap (flags cleared) because std/mio/iroh and
// the host toolkit cannot create close-on-exec sockets atomically on macOS.
#![allow(unsafe_code)]
mod support;
use budgets::{BudgetId as B, Entries};
use control_runtime::Store;
use node::{
    guardian::{PersistentChild, WorkerCommand},
    process::NativeChild,
    recovery::GroupChild,
    *,
};
use rustix::{
    io::{FdFlags, fcntl_dupfd_cloexec, fcntl_getfd, fcntl_setfd},
    process::{Resource, Rlimit, getrlimit, setrlimit},
};
use std::{
    io::Read,
    net::{TcpListener, TcpStream, UdpSocket},
    os::{fd::BorrowedFd, unix::fs::FileTypeExt, unix::fs::MetadataExt},
    path::{Path, PathBuf},
    process::Command,
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant},
};
// These tests inspect this process's descriptor table and fork repeatedly. A fork from a
// concurrent test thread holds a copy of the gap descriptors until its own exec, which is
// the separate unavoidable fork-to-exec interval, so they must not overlap.
static SERIAL: std::sync::Mutex<()> = std::sync::Mutex::new(());
static NEXT: AtomicU64 = AtomicU64::new(0);
fn serial() -> std::sync::MutexGuard<'static, ()> {
    SERIAL
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}
fn duration(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn wait(mut condition: impl FnMut() -> bool) {
    let end = Instant::now() + duration(B::WorkerStartup);
    while !condition() {
        assert!(Instant::now() < end, "fixture deadline");
        std::thread::sleep(duration(B::OwnerPoll));
    }
}
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l6-fd-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::create_dir(&p).unwrap();
        Self(p)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
fn is_open(fd: i32) -> bool {
    // SAFETY: the borrow lasts only for this `fcntl(F_GETFD)`, which is defined for any
    // integer (EBADF when closed); nothing is closed or owned through it.
    fcntl_getfd(unsafe { BorrowedFd::borrow_raw(fd) }).is_ok()
}
/// Worker side. Report every open descriptor, then stay alive until stopped.
#[test]
fn fd_report_fixture() {
    let Some(report) = std::env::var_os("L6_FD_REPORT") else {
        return;
    };
    support::require_anchor();
    let limit = getrlimit(Resource::Nofile).current.unwrap_or(4096);
    let null = std::fs::metadata("/dev/null").unwrap().rdev();
    let mut lines = String::new();
    for fd in 0..i32::try_from(limit).unwrap_or(i32::MAX) {
        if !is_open(fd) {
            continue;
        }
        let kind = match std::fs::metadata(format!("/dev/fd/{fd}")) {
            Ok(m) if m.file_type().is_char_device() && m.rdev() == null => "null",
            Ok(_) => "other",
            Err(_) => "unknown",
        };
        lines.push_str(&format!("{fd} {kind}\n"));
    }
    let report = PathBuf::from(report);
    let partial = report.with_extension("partial");
    std::fs::write(&partial, lines).unwrap();
    std::fs::rename(&partial, &report).unwrap();
    support::hold()
}
fn fixture_arguments() -> Vec<std::ffi::OsString> {
    ["--exact", "fd_report_fixture", "--nocapture"]
        .map(Into::into)
        .into()
}
fn fixture_command(report: &Path) -> Command {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(fixture_arguments())
        .env("L6_FD_REPORT", report);
    support::anchored(&mut command);
    command
}
fn descriptors_reported(report: &Path) -> String {
    wait(|| report.exists());
    std::fs::read_to_string(report).unwrap()
}
const INTENDED: &str = "0 null\n1 null\n2 null\n";
/// Parent descriptors a careless/concurrent creator would leave inheritable.
struct Gap {
    _udp: UdpSocket,
    _listener: TcpListener,
    _file: std::fs::File,
}
fn gap() -> Gap {
    let udp = UdpSocket::bind("127.0.0.1:0").unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let file = std::fs::File::open("/dev/zero").unwrap();
    fcntl_setfd(&udp, FdFlags::empty()).unwrap();
    fcntl_setfd(&listener, FdFlags::empty()).unwrap();
    fcntl_setfd(&file, FdFlags::empty()).unwrap();
    Gap {
        _udp: udp,
        _listener: listener,
        _file: file,
    }
}
/// Round 10: the confinement enumerates the open descriptors instead of scanning up to the
/// limit, so descriptors far above the low range must still be confined. The soft limit is
/// raised (never lowered: other tests share this process) so those numbers exist.
fn high_gap() -> Vec<std::os::fd::OwnedFd> {
    let limits = getrlimit(Resource::Nofile);
    let soft = limits.maximum.map_or(4096, |hard| hard.min(4096));
    if limits.current.is_some_and(|current| current < soft) {
        setrlimit(
            Resource::Nofile,
            Rlimit {
                current: Some(soft),
                maximum: limits.maximum,
            },
        )
        .unwrap();
    }
    let source = std::fs::File::open("/dev/zero").unwrap();
    let mut high = Vec::new();
    for min in [300, 700, 1500, 3000] {
        let fd = fcntl_dupfd_cloexec(&source, min).unwrap();
        assert!(std::os::fd::AsRawFd::as_raw_fd(&fd) >= min);
        fcntl_setfd(&fd, FdFlags::empty()).unwrap();
        high.push(fd);
    }
    high
}
fn ticket() -> Ticket {
    Ticket {
        epoch: 1,
        run: 1,
        node: "owned".into(),
        incarnation: "fd".into(),
    }
}
fn stopped(worker: &mut dyn WorkerAdapter) {
    worker.request_stop().unwrap();
    wait(|| match worker.observe() {
        Ok(residue) => residue.process_gone && residue.resources_gone,
        // Darwin may deny a probe of an all-zombie group; retry within the deadline.
        Err(WorkerError::Io(e)) if e.kind() == std::io::ErrorKind::PermissionDenied => false,
        Err(e) => panic!("cleanup failed: {e:?}"),
    });
}
fn native(report: &Path, root: &Path) -> Box<dyn WorkerAdapter> {
    let child = NativeChild::spawn(&mut fixture_command(report), root).unwrap();
    let pid = child.pid();
    Box::new(support::Reaped::new(child, vec![pid]))
}
fn group(report: &Path, root: &Path, db: &Path, name: &str) -> Box<dyn WorkerAdapter> {
    let mut store = Store::open(db).unwrap();
    let child = GroupChild::spawn(
        &mut fixture_command(report),
        root,
        &mut store,
        name,
        ticket(),
    )
    .unwrap();
    let pid = child.pid();
    Box::new(support::Reaped::new(child, vec![pid]))
}
fn persistent(report: &Path, base: &Path, name: &str) -> Box<dyn WorkerAdapter> {
    let worker = WorkerCommand {
        program: std::env::current_exe().unwrap(),
        arguments: fixture_arguments(),
        environment: vec![
            ("L6_FD_REPORT".into(), Some(report.as_os_str().into())),
            (support::ANCHOR.into(), Some(support::anchor_value())),
        ],
    };
    let child = PersistentChild::spawn(
        Path::new(env!("CARGO_BIN_EXE_node-guardian")),
        &worker,
        &base.join(format!("{name}-resources")),
        &base.join("state.db"),
        name,
        ticket(),
    )
    .unwrap();
    let (guardian, worker) = child.process_ids().unwrap();
    Box::new(support::Reaped::new(child, vec![guardian, worker]))
}
#[test]
fn native_worker_receives_exactly_stdio_and_no_parent_descriptor() {
    let _serial = serial();
    let t = Temp::new();
    let _gap = gap();
    let report = t.0.join("report");
    let mut worker = native(&report, &t.0.join("worker"));
    assert_eq!(descriptors_reported(&report), INTENDED);
    stopped(&mut *worker);
}
#[test]
fn group_worker_receives_exactly_stdio_and_no_parent_descriptor() {
    let _serial = serial();
    let t = Temp::new();
    let _gap = gap();
    let report = t.0.join("report");
    let mut worker = group(&report, &t.0.join("worker"), &t.0.join("state.db"), "group");
    assert_eq!(descriptors_reported(&report), INTENDED);
    stopped(&mut *worker);
}
#[test]
fn guardian_worker_receives_exactly_stdio_and_no_parent_descriptor() {
    let _serial = serial();
    let t = Temp::new();
    let _gap = gap();
    let report = t.0.join("report");
    let mut worker = persistent(&report, &t.0, "guarded");
    assert_eq!(descriptors_reported(&report), INTENDED);
    stopped(&mut *worker);
}
#[test]
fn workers_receive_no_parent_descriptor_even_far_above_the_low_range() {
    let _serial = serial();
    let _gap = gap();
    let _high = high_gap();
    let t = Temp::new();
    let mut native_worker = native(&t.0.join("native-report"), &t.0.join("native"));
    assert_eq!(descriptors_reported(&t.0.join("native-report")), INTENDED);
    stopped(&mut *native_worker);
    let mut group_worker = group(
        &t.0.join("group-report"),
        &t.0.join("group"),
        &t.0.join("state.db"),
        "group",
    );
    assert_eq!(descriptors_reported(&t.0.join("group-report")), INTENDED);
    stopped(&mut *group_worker);
    let mut guarded = persistent(&t.0.join("guarded-report"), &t.0, "guarded");
    assert_eq!(descriptors_reported(&t.0.join("guarded-report")), INTENDED);
    stopped(&mut *guarded);
}
/// A listener generation: closing it must free the address and FIN every accepted peer
/// even though a worker spawned while it was open is still alive.
fn rollover_generations(mut spawn: impl FnMut(usize) -> Box<dyn WorkerAdapter>) {
    let mut workers: Vec<Box<dyn WorkerAdapter>> = Vec::new();
    let mut listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    for generation in 0..3 {
        fcntl_setfd(&listener, FdFlags::empty()).unwrap();
        let client = TcpStream::connect(address).unwrap();
        let (accepted, _) = listener.accept().unwrap();
        fcntl_setfd(&accepted, FdFlags::empty()).unwrap();
        workers.push(spawn(generation));
        // Owner restart/rollover: the old listener and its connection close while the
        // previous generations' workers are still running.
        drop(accepted);
        drop(listener);
        client
            .set_read_timeout(Some(duration(B::WorkerStartup)))
            .unwrap();
        let mut byte = [0u8; 1];
        // A read timeout surfaces as WouldBlock on macOS: the connection stayed open.
        match (&client).read(&mut byte) {
            Ok(0) => {}
            other => panic!(
                "generation {generation}: closed connection was not EOF at the peer \
                 (a worker still holds it): {other:?}"
            ),
        }
        listener = TcpListener::bind(address)
            .unwrap_or_else(|e| panic!("generation {generation} cannot rebind immediately: {e}"));
        for worker in &mut workers {
            assert!(!worker.observe().unwrap().process_gone, "worker outlived");
        }
    }
    drop(listener);
    for worker in &mut workers {
        stopped(&mut **worker);
    }
}
#[test]
fn public_boundary_confines_a_caller_owned_command() {
    let _serial = serial();
    let t = Temp::new();
    let _gap = gap();
    let report = t.0.join("report");
    let mut command = fixture_command(&report);
    command
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    node::process::confine_descriptors(&mut command).unwrap();
    let child = support::Owned(command.spawn().unwrap());
    let observed = descriptors_reported(&report);
    drop(child);
    assert_eq!(observed, INTENDED);
}
#[test]
fn native_worker_that_outlives_the_listener_never_blocks_immediate_rebind() {
    let _serial = serial();
    let t = Temp::new();
    rollover_generations(|n| {
        native(
            &t.0.join(format!("report-{n}")),
            &t.0.join(format!("worker-{n}")),
        )
    });
}
#[test]
fn group_worker_that_outlives_the_listener_never_blocks_immediate_rebind() {
    let _serial = serial();
    let t = Temp::new();
    rollover_generations(|n| {
        group(
            &t.0.join(format!("report-{n}")),
            &t.0.join(format!("worker-{n}")),
            &t.0.join("state.db"),
            &format!("group-{n}"),
        )
    });
}
#[test]
fn guardian_and_worker_that_outlive_the_listener_never_block_immediate_rebind() {
    let _serial = serial();
    let t = Temp::new();
    rollover_generations(|n| {
        persistent(
            &t.0.join(format!("report-{n}")),
            &t.0,
            &format!("guarded-{n}"),
        )
    });
}
