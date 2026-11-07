// SPDX-License-Identifier: AGPL-3.0-only
// P:path:mycelium_node/process.py (Python `subprocess` close_fds=True: a child never kept a
// parent socket). macOS cannot create a socket close-on-exec atomically, so every test forces
// the descriptors into that gap (FD_CLOEXEC cleared) and then spawns a REAL helper child that
// reports which of those descriptor numbers it can still see. Real children only; no device,
// app, network peer or fixed port (loopback ephemeral sockets).
#![allow(unsafe_code)]
mod support;
use budgets::Span;
use device_harness::{
    effects::{Command, CommandKind, Transport},
    native::NativeTransport,
};
use std::{
    net::{TcpListener, TcpStream, UdpSocket},
    os::{
        fd::{AsRawFd, RawFd},
        unix::net::UnixStream,
    },
    sync::{Mutex, MutexGuard, PoisonError},
};
use support::PrivateRoot;

// A fork from a concurrent test thread holds a copy of the gap descriptors until its own
// exec (the unavoidable fork-to-exec interval), so these tests must not overlap.
static SERIAL: Mutex<()> = Mutex::new(());
fn serial() -> MutexGuard<'static, ()> {
    SERIAL.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Clear FD_CLOEXEC, exactly the state of a socket in the creation gap.
fn leak(fd: RawFd) {
    // SAFETY: `fcntl(F_GETFD/F_SETFD)` on a descriptor owned by a live socket in this test.
    unsafe {
        let flags = libc::fcntl(fd, libc::F_GETFD);
        assert!(flags >= 0, "F_GETFD");
        assert_eq!(libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC), 0);
    }
}

/// Parent-held sockets of every kind a harness process owns, all non-cloexec.
struct Sockets {
    listener: TcpListener,
    client: TcpStream,
    accepted: TcpStream,
    udp: UdpSocket,
    pair: (UnixStream, UnixStream),
}
impl Sockets {
    fn new() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let client = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        let (accepted, _) = listener.accept().unwrap();
        let udp = UdpSocket::bind("127.0.0.1:0").unwrap();
        let pair = UnixStream::pair().unwrap();
        let s = Self {
            listener,
            client,
            accepted,
            udp,
            pair,
        };
        for fd in s.fds() {
            leak(fd);
        }
        s
    }
    fn fds(&self) -> Vec<RawFd> {
        vec![
            self.listener.as_raw_fd(),
            self.client.as_raw_fd(),
            self.accepted.as_raw_fd(),
            self.udp.as_raw_fd(),
            self.pair.0.as_raw_fd(),
            self.pair.1.as_raw_fd(),
        ]
    }
}

/// The helper prints `held:N` for each given descriptor number that is open in the child.
const PROBE: &str = r#"for fd in "$@"; do if [ -e "/dev/fd/$fd" ]; then echo "held:$fd"; fi; done; echo probe-done"#;

fn probe_args(fds: &[RawFd]) -> Vec<String> {
    let mut args = vec!["-c".to_string(), PROBE.to_string(), "probe".to_string()];
    args.extend(fds.iter().map(ToString::to_string));
    args
}
fn held(stdout: &str) -> Vec<String> {
    assert!(
        stdout.contains("probe-done"),
        "probe did not run: {stdout:?}"
    );
    stdout
        .lines()
        .filter(|l| l.starts_with("held:"))
        .map(str::to_string)
        .collect()
}

#[test]
fn control_unconfined_spawn_does_inherit_the_gap_sockets() {
    // The fixture must be able to see an inherited descriptor, or the confinement test below
    // would be vacuous.
    let _guard = serial();
    let sockets = Sockets::new();
    let fds = sockets.fds();
    let out = std::process::Command::new("/bin/sh")
        .args(probe_args(&fds))
        .output()
        .unwrap();
    let seen = held(&String::from_utf8(out.stdout).unwrap());
    assert_eq!(
        seen.len(),
        fds.len(),
        "fixture cannot observe inheritance: {seen:?}"
    );
}

#[test]
fn native_transport_probe_holds_no_parent_socket() {
    let _guard = serial();
    let root = PrivateRoot::new("fd-confine");
    let sockets = Sockets::new();
    let fds = sockets.fds();
    let mut transport = NativeTransport {
        scratch: root.to_path_buf(),
        uid: root.uid(),
        poll: Span::from_millis(20),
        max_output_bytes: 4096,
        diagnostics: vec![],
    };
    let out = transport
        .execute(
            &Command {
                kind: CommandKind::Local,
                program: "/bin/sh".into(),
                args: probe_args(&fds),
            },
            Span::from_millis(10_000),
        )
        .unwrap();
    assert_eq!(out.rc, 0, "{}", out.stderr);
    assert_eq!(held(&out.stdout), Vec::<String>::new());
}

#[test]
fn confinement_does_not_break_stdio_capture_or_a_failed_exec_report() {
    let _guard = serial();
    let root = PrivateRoot::new("fd-confine-stdio");
    let mut transport = NativeTransport {
        scratch: root.to_path_buf(),
        uid: root.uid(),
        poll: Span::from_millis(20),
        max_output_bytes: 4096,
        diagnostics: vec![],
    };
    let ok = transport
        .execute(
            &Command {
                kind: CommandKind::Local,
                program: "/bin/sh".into(),
                args: vec!["-c".into(), "echo out; echo err >&2; exit 3".into()],
            },
            Span::from_millis(10_000),
        )
        .unwrap();
    assert_eq!(
        (ok.rc, ok.stdout.trim(), ok.stderr.trim()),
        (3, "out", "err")
    );
    // std's close-on-exec error pipe must survive: a missing program is a spawn failure,
    // never a silent success.
    let err = transport
        .execute(
            &Command {
                kind: CommandKind::Local,
                program: root.join("no-such-program").display().to_string(),
                args: vec![],
            },
            Span::from_millis(10_000),
        )
        .err()
        .unwrap();
    assert_eq!(err.code, "command_spawn_failed");
}
