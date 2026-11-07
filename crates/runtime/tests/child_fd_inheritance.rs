// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-HYG-03 (a native child receives exactly the pass list and nothing else, including descriptors created in the socket()/CLOEXEC gap).
//!
//! Production child-process descriptor inheritance (rounds 6-7).
//!
//! The canonical confinement is `node::process::confine_descriptors`, applied by
//! `NativeChild::spawn` itself (L6 owns it; `runtime` carries no second copy, see
//! `spawn_boundary_ownership.rs`). These tests drive that production spawn with a
//! caller-built plain `Command` and pin the exposure it removes against a bare
//! `std::process::Command::spawn`.
//!
//! v0.1.4 spawned every child through Python `subprocess`, whose default
//! `close_fds=True` (and `pass_fds` for the few intended descriptors) means a
//! child never keeps a parent socket, even if the socket was created in the
//! `socket()`/`FD_CLOEXEC` gap that macOS cannot close atomically. Rust's
//! `std::process::Command` has no such default: a descriptor that is not yet
//! close-on-exec when the child is forked survives `exec` for the child's whole
//! life, so a port the parent closed stays bound ("AddrInUse" on rebind).
use budgets::{BudgetId as B, Entries};
use node::{
    WorkerAdapter,
    process::{NativeChild, confine_descriptors},
};
use rustix::{
    io::{Errno, FdFlags, fcntl_getfd, fcntl_setfd},
    process::{Pid, Signal, getppid, kill_process, test_kill_process},
};
use std::{
    collections::BTreeSet,
    io::{BufRead, BufReader, ErrorKind},
    net::{SocketAddr, TcpListener, UdpSocket},
    os::fd::AsRawFd,
    panic::{AssertUnwindSafe, catch_unwind, resume_unwind},
    path::PathBuf,
    process::{Command, Stdio},
    sync::{
        Mutex, MutexGuard,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
// These tests assert descriptor tables of this whole process, so they must not
// overlap each other (a concurrent fork would add the separate S23 release
// window to what is measured here).
static SERIAL: Mutex<()> = Mutex::new(());
static NEXT: AtomicU64 = AtomicU64::new(0);
fn serial() -> MutexGuard<'static, ()> {
    SERIAL
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}
fn budget(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
/// Environment value naming the process a fixture child is anchored to.
const ANCHOR: &str = "L9_FDS_FIXTURE_ANCHOR";
/// Set only on the re-executed owner process of the owner-death test.
const OWNER: &str = "L9_FDS_FIXTURE_OWNER";
/// A fixture child: this very test binary re-executed as an idle holder that
/// exits by itself once the process that spawned it is gone (its parent
/// changes), so no path (pass, assertion, panic, killed test process) leaves
/// it behind. It owns no socket of its own, so every UDP descriptor the
/// kernel lists for it was inherited.
fn sleeper() -> Command {
    let mut command = Command::new(std::env::current_exe().expect("this test binary"));
    command
        .args(["--exact", "hold_fixture", "--nocapture", "--test-threads=1"])
        .env(ANCHOR, std::process::id().to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    command
}
fn pid_of(raw: u32) -> Pid {
    Pid::from_raw(i32::try_from(raw).expect("pid fits")).expect("non-zero pid")
}
/// Fixture side: stay alive while the process this fixture was spawned by lives.
fn hold_until_orphaned(anchor: u32) {
    let anchor = i32::try_from(anchor).expect("pid fits");
    while getppid().map(Pid::as_raw_nonzero).map(|pid| pid.get()) == Some(anchor) {
        std::thread::sleep(budget(B::OwnerPoll));
    }
}
/// Not a test: the body a fixture child runs. It returns at once (and passes)
/// unless it was launched as a fixture.
#[test]
fn hold_fixture() {
    if let Some(anchor) = std::env::var(ANCHOR).ok().and_then(|v| v.parse().ok()) {
        hold_until_orphaned(anchor);
    }
}
fn gone(pid: u32) -> bool {
    match test_kill_process(pid_of(pid)) {
        Err(Errno::SRCH) => true,
        Ok(()) | Err(Errno::PERM) => false,
        Err(error) => panic!("pid observation failed: {error}"),
    }
}
/// Poll until every pid is gone or `within` elapses; returns the survivors
/// after killing each by its exact pid (a failed run must not leave them).
fn wait_gone_then_kill_survivors(pids: &[u32], within: Duration) -> Vec<u32> {
    let end = Instant::now() + within;
    while Instant::now() < end && !pids.iter().all(|pid| gone(*pid)) {
        std::thread::sleep(budget(B::OwnerPoll));
    }
    let survivors: Vec<u32> = pids.iter().copied().filter(|pid| !gone(*pid)).collect();
    for pid in &survivors {
        if let Err(error) = kill_process(pid_of(*pid), Signal::KILL) {
            eprintln!("kill of exact pid {pid} failed: {error}");
        }
    }
    survivors
}
fn root() -> PathBuf {
    std::env::temp_dir().join(format!(
        "v02-l9-fds-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ))
}
enum Held {
    Native(NativeChild),
    Bare(std::process::Child),
}
impl Held {
    fn pid(&self) -> u32 {
        match self {
            Self::Native(child) => child.pid(),
            Self::Bare(child) => child.id(),
        }
    }
    /// Stop the exact owned child and wait until it (and a native child's
    /// directory) is gone.
    fn stop(&mut self) -> Result<(), String> {
        match self {
            Self::Native(child) => {
                child.request_stop().map_err(|e| format!("stop: {e:?}"))?;
                let end = Instant::now() + budget(B::RouterClose);
                loop {
                    let residue = child.observe().map_err(|e| format!("observe: {e:?}"))?;
                    if residue.process_gone && residue.resources_gone {
                        return Ok(());
                    }
                    if Instant::now() >= end {
                        return Err("owned fixture did not stop in time".into());
                    }
                    std::thread::yield_now();
                }
            }
            Self::Bare(child) => {
                child.kill().map_err(|e| format!("kill: {e}"))?;
                child.wait().map(drop).map_err(|e| format!("wait: {e}"))
            }
        }
    }
}
/// Owns one fixture child and reaps it on every path: `reap` asserts, `Drop`
/// (a failed assertion, a panic, an early return) stops the exact pid too.
struct Reaped(Option<Held>);
impl Reaped {
    fn native(command: &mut Command) -> Self {
        Self(Some(Held::Native(
            NativeChild::spawn(command, &root()).unwrap(),
        )))
    }
    fn bare(command: &mut Command) -> Self {
        Self(Some(Held::Bare(command.spawn().unwrap())))
    }
    fn adopt(child: std::process::Child) -> Self {
        Self(Some(Held::Bare(child)))
    }
    fn pid(&self) -> u32 {
        self.0.as_ref().expect("not yet reaped").pid()
    }
    fn reap(mut self) {
        let mut held = self.0.take().expect("not yet reaped");
        let pid = held.pid();
        if let Err(reason) = held.stop() {
            panic!("fixture {pid} not reaped: {reason}");
        }
        assert!(gone(pid), "fixture {pid} outlived its reap");
    }
}
impl Drop for Reaped {
    fn drop(&mut self) {
        // Never panics (this may run while unwinding): stop the exact pid, ignore errors.
        if let Some(mut held) = self.0.take()
            && let Err(reason) = held.stop()
        {
            eprintln!("fixture {} not stopped on drop: {reason}", held.pid());
        }
    }
}
/// A descriptor in the creation gap: created, close-on-exec not yet applied.
fn gap_state(socket: &impl std::os::fd::AsFd) {
    let flags = fcntl_getfd(socket).unwrap();
    assert!(
        flags.contains(FdFlags::CLOEXEC),
        "std marks new sockets close-on-exec (after socket(2) on macOS)"
    );
    fcntl_setfd(socket, FdFlags::empty()).unwrap();
}
fn udp_gap_socket() -> (UdpSocket, SocketAddr) {
    let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
    let address = socket.local_addr().unwrap();
    gap_state(&socket);
    (socket, address)
}
fn rebind_is_blocked(address: SocketAddr) -> bool {
    match UdpSocket::bind(address) {
        Ok(_) => false,
        Err(error) => error.kind() == ErrorKind::AddrInUse,
    }
}
#[test]
fn bare_std_spawn_keeps_a_gap_socket_after_parent_close() {
    let _serial = serial();
    let (socket, address) = udp_gap_socket();
    let child = Reaped::bare(&mut sleeper());
    drop(socket);
    // Pins the exposure the canonical boundary removes (and proves the gap
    // fixture is not vacuous): the parent closed the socket, the unrelated
    // child still holds the port.
    let blocked = rebind_is_blocked(address);
    child.reap();
    assert!(blocked, "child did not inherit the socket");
    assert!(
        UdpSocket::bind(address).is_ok(),
        "port frees once child exits"
    );
}
#[test]
fn native_child_releases_a_gap_udp_socket_for_a_plain_command() {
    let _serial = serial();
    let (socket, address) = udp_gap_socket();
    let owned = Reaped::native(&mut sleeper());
    drop(socket);
    assert!(
        UdpSocket::bind(address).is_ok(),
        "worker inherited a parent UDP socket across exec"
    );
    owned.reap();
}
#[test]
fn native_child_releases_a_gap_tcp_listener_for_a_plain_command() {
    let _serial = serial();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    gap_state(&listener);
    let owned = Reaped::native(&mut sleeper());
    drop(listener);
    assert!(
        TcpListener::bind(address).is_ok(),
        "worker inherited a parent listener across exec"
    );
    owned.reap();
}
#[test]
fn confinement_keeps_stdio_and_still_reports_exec_failure() {
    let _serial = serial();
    let mut command = Command::new("/nonexistent/v02-l9-no-such-program");
    let Err(error) = NativeChild::spawn(&mut command, &root()) else {
        panic!("a missing executable must fail the spawn");
    };
    // std's close-on-exec error pipe must survive: a failed exec is a failed
    // spawn, never a silent success.
    assert!(
        format!("{error:?}").contains("NotFound"),
        "exec failure was not reported: {error:?}"
    );
    let output = {
        let mut echo = Command::new("/bin/echo");
        echo.arg("stdio-survives");
        confine_descriptors(&mut echo).unwrap();
        echo.output().unwrap()
    };
    assert_eq!(output.stdout, b"stdio-survives\n");
}
/// UDP descriptors held by the given live pids, read from the kernel's own
/// per-process descriptor table (no rebind probe, so unrelated processes using
/// the same ephemeral ports cannot be mistaken for a leak).
fn udp_descriptors_held_by(pids: &[u32]) -> usize {
    let list = pids
        .iter()
        .map(u32::to_string)
        .collect::<Vec<_>>()
        .join(",");
    let output = Command::new("/usr/sbin/lsof")
        .args(["-nP", "-a", "-i", "UDP", "-Fn", "-p", &list])
        .output()
        .expect("lsof runs");
    // lsof exits 1 with no output when no listed pid holds a UDP descriptor.
    assert!(output.status.success() || output.stdout.is_empty());
    String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .filter(|line| line.starts_with('n'))
        .count()
}
#[test]
fn descriptor_probe_sees_a_known_udp_socket() {
    let _serial = serial();
    let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
    assert!(udp_descriptors_held_by(&[std::process::id()]) >= 1);
    drop(socket);
}
/// One parent descriptor of every kind a worker could wrongly inherit, all with
/// close-on-exec cleared (the worst case a spawn can meet): UDP socket, TCP
/// listener and both ends of a TCP stream, both ends of a Unix socket pair, a
/// regular file and both ends of a pipe. Held open for the life of this value.
struct Decoys {
    held: Vec<std::os::fd::OwnedFd>,
}
impl Decoys {
    fn open() -> Self {
        let udp = UdpSocket::bind("127.0.0.1:0").unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let client = std::net::TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        let (server, _) = listener.accept().unwrap();
        let (unix_a, unix_b) = std::os::unix::net::UnixStream::pair().unwrap();
        let file = std::fs::File::open(std::env::current_exe().unwrap()).unwrap();
        let (pipe_read, pipe_write) = rustix::pipe::pipe().unwrap();
        let held: Vec<std::os::fd::OwnedFd> = vec![
            udp.into(),
            listener.into(),
            client.into(),
            server.into(),
            unix_a.into(),
            unix_b.into(),
            file.into(),
            pipe_read,
            pipe_write,
        ];
        for descriptor in &held {
            fcntl_setfd(descriptor, FdFlags::empty()).unwrap();
        }
        Self { held }
    }
    fn numbers(&self) -> BTreeSet<i32> {
        self.held.iter().map(AsRawFd::as_raw_fd).collect()
    }
    fn number(&self, index: usize) -> i32 {
        self.held[index].as_raw_fd()
    }
}
/// The descriptor numbers a live process holds, from the kernel's own table
/// (`lsof -Ff`; the `cwd`/`txt` pseudo-entries are not descriptors).
fn descriptor_numbers_of(pid: u32) -> BTreeSet<i32> {
    let output = Command::new("/usr/sbin/lsof")
        .args(["-nP", "-Ff", "-p", &pid.to_string()])
        .output()
        .expect("lsof runs");
    assert!(output.status.success(), "lsof could not read pid {pid}");
    String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .filter_map(|line| line.strip_prefix('f'))
        .filter_map(|name| name.parse().ok())
        .collect()
}
/// Spawn a fixture with `pass` handed through the production boundary.
fn native_passing(pass: &[i32]) -> Reaped {
    Reaped(Some(Held::Native(
        NativeChild::spawn_passing(&mut sleeper(), &root(), pass).unwrap(),
    )))
}
#[test]
fn bare_std_child_holds_every_parent_descriptor_so_the_table_check_is_not_vacuous() {
    let _serial = serial();
    let decoys = Decoys::open();
    let child = Reaped::bare(&mut sleeper());
    let held = descriptor_numbers_of(child.pid());
    child.reap();
    let missing: Vec<i32> = decoys.numbers().difference(&held).copied().collect();
    assert!(
        missing.is_empty(),
        "bare spawn did not carry decoys {missing:?}; the inverse check below would prove nothing"
    );
}
/// Inverse of the removed `plain_command_through_native_child_keeps_a_gap_socket_after_parent_close`.
#[test]
fn native_child_receives_no_descriptor_beyond_stdio() {
    let _serial = serial();
    let decoys = Decoys::open();
    let child = Reaped::native(&mut sleeper());
    let held = descriptor_numbers_of(child.pid());
    child.reap();
    assert_eq!(
        held,
        BTreeSet::from([0, 1, 2]),
        "worker inherited parent descriptors (decoys were {:?})",
        decoys.numbers()
    );
}
#[test]
fn native_child_receives_exactly_the_pass_list_and_nothing_else() {
    let _serial = serial();
    let decoys = Decoys::open();
    let passed = [decoys.number(7), decoys.number(0)];
    let child = native_passing(&passed);
    let held = descriptor_numbers_of(child.pid());
    child.reap();
    let expected: BTreeSet<i32> = [0, 1, 2].into_iter().chain(passed).collect();
    assert_eq!(
        held,
        expected,
        "worker holds more or less than the pass list (decoys were {:?})",
        decoys.numbers()
    );
}
/// How a child is spawned in the natural-race measurement.
#[derive(Clone, Copy, Debug)]
enum Spawn {
    /// Production `NativeChild::spawn` (confines internally).
    Native,
    /// Bare `std::process::Command::spawn`, the unconfined exposure.
    BareStd,
}
/// Threaded natural race: sockets are created (and dropped) continuously while
/// children are spawned. A child forked inside a socket's creation gap keeps
/// that socket for its whole life; count UDP descriptors alive in the children
/// after every spawn returned and the creator stopped.
fn leaked_into_children(spawn: Spawn, spawns: usize) -> usize {
    let stop = AtomicBool::new(false);
    let mut created = 0;
    let mut children = Vec::new();
    std::thread::scope(|scope| {
        let creator = scope.spawn(|| {
            let mut count = 0_usize;
            while !stop.load(Ordering::SeqCst) {
                drop(UdpSocket::bind("127.0.0.1:0").unwrap());
                count += 1;
            }
            count
        });
        for _ in 0..spawns {
            children.push(match spawn {
                Spawn::Native => Reaped::native(&mut sleeper()),
                Spawn::BareStd => Reaped::bare(&mut sleeper()),
            });
        }
        stop.store(true, Ordering::SeqCst);
        created = creator.join().unwrap();
    });
    let pids: Vec<u32> = children.iter().map(Reaped::pid).collect();
    let leaked = udp_descriptors_held_by(&pids);
    for child in children {
        child.reap();
    }
    eprintln!("spawn={spawn:?} spawns={spawns} sockets_created={created} leaked={leaked}");
    leaked
}
#[test]
fn concurrent_creation_gap_never_leaks_into_native_children() {
    let _serial = serial();
    assert_eq!(leaked_into_children(Spawn::Native, 120), 0);
}
#[test]
fn concurrent_creation_gap_bare_std_rate_is_observed() {
    let _serial = serial();
    // Observation only: the natural race is probabilistic, the deterministic
    // pin is the gap-state test above.
    let leaked = leaked_into_children(Spawn::BareStd, 120);
    eprintln!("bare std leaked={leaked}");
}
/// Owner side of the owner-death test: not a test unless launched as the owner.
/// It spawns two fixtures (one confined, one bare), announces their pids and
/// parks until its own spawner is gone.
#[test]
fn own_fixtures_then_park() {
    if std::env::var_os(OWNER).is_none() {
        return;
    }
    let native = Reaped::native(&mut sleeper());
    let bare = Reaped::bare(&mut sleeper());
    // libtest has just printed "test <name> ... " with no newline.
    println!("\nFIXTURES {} {}", native.pid(), bare.pid());
    std::io::Write::flush(&mut std::io::stdout()).unwrap();
    if let Some(anchor) = std::env::var(ANCHOR).ok().and_then(|v| v.parse().ok()) {
        hold_until_orphaned(anchor);
    }
}
#[test]
fn a_panicking_test_leaves_no_fixture_behind() {
    let _serial = serial();
    let pids = Mutex::new(Vec::new());
    let result = catch_unwind(AssertUnwindSafe(|| {
        let native = Reaped::native(&mut sleeper());
        let bare = Reaped::bare(&mut sleeper());
        pids.lock().unwrap().extend([native.pid(), bare.pid()]);
        // Unwinds like a failed assertion, without the panic-hook noise.
        resume_unwind(Box::new("injected test failure"));
    }));
    assert!(result.is_err());
    let pids = pids.into_inner().unwrap();
    assert_eq!(pids.len(), 2);
    let survivors = wait_gone_then_kill_survivors(&pids, budget(B::RouterClose));
    assert!(
        survivors.is_empty(),
        "fixtures outlived the unwinding test: {survivors:?}"
    );
}
#[test]
fn fixtures_exit_when_their_owning_process_is_killed() {
    let _serial = serial();
    let mut owner = Command::new(std::env::current_exe().expect("this test binary"));
    owner
        .args([
            "--exact",
            "own_fixtures_then_park",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(OWNER, "1")
        .env(ANCHOR, std::process::id().to_string())
        .stdout(Stdio::piped());
    let mut child = owner.spawn().unwrap();
    let stdout = child.stdout.take().expect("piped owner stdout");
    let owner = Reaped::adopt(child);
    let (sender, receiver) = std::sync::mpsc::channel();
    let reader = std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if let Some(rest) = line.strip_prefix("FIXTURES ") {
                if sender.send(rest.to_owned()).is_err() {
                    eprintln!("announcement receiver is gone");
                }
                return;
            }
        }
    });
    let announced = receiver.recv_timeout(budget(B::WorkerStartup));
    let pids: Vec<u32> = announced
        .expect("owner announced its fixtures")
        .split(' ')
        .map(|pid| pid.parse().unwrap())
        .collect();
    assert_eq!(pids.len(), 2);
    assert!(pids.iter().all(|pid| !gone(*pid)), "fixtures not running");
    // The owner is killed without running any destructor.
    owner.reap();
    reader.join().unwrap();
    let survivors = wait_gone_then_kill_survivors(&pids, budget(B::WorkerStartup));
    assert!(
        survivors.is_empty(),
        "fixtures outlived their killed owner: {survivors:?}"
    );
}
