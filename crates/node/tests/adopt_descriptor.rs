// SPDX-License-Identifier: AGPL-3.0-only
// Seam S-L9-2: `node::fds::adopt_inherited_descriptor` in a real exec'd child. The child is
// this test binary re-executed with a descriptor deliberately left inheritable, exactly as
// L9's sidecar finds its `--bootstrap-fd N`. v0.1.4 at the same point: a closed number
// aborts the process (exit 134, `L9-evidence/r9-oracle-closed-fd.txt`); a non-FIFO is
// `bootstrap_not_pipe`; a negative number is `invalid_bootstrap_fd`.
#![allow(unsafe_code)]
mod support;
use node::fds::{DescriptorKind, adopt_inherited_descriptor, refusal};
use rustix::{
    fs::fstat,
    io::{FdFlags, fcntl_getfd, fcntl_setfd},
};
use std::{
    io::{Read, Write},
    os::{
        fd::{AsRawFd, BorrowedFd, OwnedFd},
        unix::process::{CommandExt, ExitStatusExt},
    },
    path::{Path, PathBuf},
    process::{Command, ExitStatus, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    time::Instant,
};
static NEXT: AtomicU64 = AtomicU64::new(0);
/// What the caller does on a refusal (L9 chooses its own code; any non-zero, non-abort).
const REFUSED_EXIT: i32 = 3;
const SECRET: [u8; 32] = [0x5a; 32];

/// Child side: adopt `L6_ADOPT_FD` as `L6_ADOPT_KIND`, write the outcome to the report file
/// and exit like the sidecar would.
#[test]
fn adopt_fixture() {
    let Some(report) = std::env::var_os("L6_ADOPT_REPORT") else {
        return;
    };
    support::require_anchor();
    let fd: i32 = std::env::var("L6_ADOPT_FD").unwrap().parse().unwrap();
    let kind = match std::env::var("L6_ADOPT_KIND").unwrap().as_str() {
        "fifo" => DescriptorKind::Fifo,
        "socket" => DescriptorKind::Socket,
        "regular" => DescriptorKind::Regular,
        other => panic!("kind {other}"),
    };
    let mode = std::env::var("L6_ADOPT_MODE").unwrap_or_default();
    let mut out = String::new();
    let mut code = 0;
    match adopt_inherited_descriptor(fd, kind) {
        Ok(owned) => {
            out.push_str("adopted\n");
            // SAFETY: borrow for one fcntl on a descriptor `owned` keeps open.
            let flags = fcntl_getfd(unsafe { BorrowedFd::borrow_raw(owned.as_raw_fd()) }).unwrap();
            out.push_str(&format!("cloexec={}\n", flags.contains(FdFlags::CLOEXEC)));
            if mode == "twice" {
                // Own it for the second attempt, then let it go and try the stale number.
                let second = adopt_inherited_descriptor(fd, kind);
                out.push_str(&format!("second={}\n", describe(&second)));
                drop(owned);
                let third = adopt_inherited_descriptor(fd, kind);
                out.push_str(&format!("third={}\n", describe(&third)));
            } else {
                // A grandchild must not inherit it: it looks for the same open file (device +
                // inode, so a number a fresh process happened to reuse cannot fool it).
                let id = fstat(&owned).unwrap();
                let probe = Command::new(std::env::current_exe().unwrap())
                    .args(["--exact", "adopt_probe_fixture", "--nocapture"])
                    .env(
                        "L6_ADOPT_PROBE",
                        format!("{fd}:{}:{}", id.st_dev, id.st_ino),
                    )
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status()
                    .unwrap();
                out.push_str(&format!(
                    "grandchild_inherited={}
",
                    probe.success()
                ));
                if kind == DescriptorKind::Fifo {
                    let mut bytes = Vec::new();
                    std::fs::File::from(owned).read_to_end(&mut bytes).unwrap();
                    out.push_str(&format!("bytes={}\n", bytes.len()));
                    out.push_str(&format!("secret_ok={}\n", bytes == vec![0x5a; 32]));
                }
            }
        }
        Err(error) => {
            let why = refusal(&error).expect("refusal carries its reason");
            out.push_str(&format!(
                "refused={}\nsidecar={}\n",
                why.code(),
                why.sidecar_code()
            ));
            code = REFUSED_EXIT;
        }
    }
    let partial = Path::new(&report).with_extension("partial");
    std::fs::write(&partial, out).unwrap();
    std::fs::rename(&partial, &report).unwrap();
    std::process::exit(code);
}
/// Grandchild side: succeed iff descriptor `fd` is open here and is the file `dev`/`ino`.
#[test]
fn adopt_probe_fixture() {
    let Some(spec) = std::env::var_os("L6_ADOPT_PROBE") else {
        return;
    };
    support::require_anchor();
    let spec = spec.into_string().unwrap();
    let mut parts = spec.split(':');
    let fd: i32 = parts.next().unwrap().parse().unwrap();
    let dev: i32 = parts.next().unwrap().parse().unwrap();
    let ino: u64 = parts.next().unwrap().parse().unwrap();
    // SAFETY: one `fstat` borrow of an integer descriptor; nothing is closed or owned.
    let same = rustix::fs::fstat(unsafe { BorrowedFd::borrow_raw(fd) })
        .is_ok_and(|s| s.st_dev == dev && s.st_ino == ino);
    std::process::exit(i32::from(!same));
}
fn describe(result: &std::io::Result<OwnedFd>) -> String {
    match result {
        Ok(_) => "adopted".into(),
        Err(e) => refusal(e).map_or_else(|| format!("unclassified:{e}"), |r| r.code().into()),
    }
}
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l6-adopt-{}-{}",
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
struct Outcome {
    status: ExitStatus,
    report: String,
}
impl Outcome {
    fn has(&self, line: &str) -> bool {
        self.report.lines().any(|l| l == line)
    }
    /// Exited by itself with a non-zero status: not signalled, not the abort status.
    fn refused_cleanly(&self) -> bool {
        self.status.code() == Some(REFUSED_EXIT) && self.status.signal().is_none()
    }
}
/// Run the fixture with descriptor number `fd` in the environment. `leave_open` descriptors
/// are made inheritable in the child (flag cleared at exec); `stdio` fills 0/1/2.
fn run(fd: i32, kind: &str, mode: &str, leave_open: &[i32], stdio: [Stdio; 3]) -> Outcome {
    let t = Temp::new();
    let report = t.0.join("report");
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(["--exact", "adopt_fixture", "--nocapture"])
        .env("L6_ADOPT_REPORT", &report)
        .env("L6_ADOPT_FD", fd.to_string())
        .env("L6_ADOPT_KIND", kind)
        .env("L6_ADOPT_MODE", mode);
    support::anchored(&mut command);
    let [i, o, e] = stdio;
    command.stdin(i).stdout(o).stderr(e);
    let open: Vec<i32> = leave_open.to_vec();
    // SAFETY: runs in the forked child before exec; only `fcntl(F_SETFD)` via rustix.
    unsafe {
        command.pre_exec(move || {
            for fd in &open {
                fcntl_setfd(BorrowedFd::borrow_raw(*fd), FdFlags::empty())?;
            }
            Ok(())
        });
    }
    let child = command.spawn().unwrap();
    let pid = child.id();
    let mut child = support::Owned(child);
    let end = Instant::now() + support::bounded();
    let status = loop {
        if let Some(status) = child.0.try_wait().unwrap() {
            break status;
        }
        assert!(Instant::now() < end, "fixture {pid} deadline");
        std::thread::sleep(support::poll());
    };
    // The report is written before exit unless the process aborted first.
    let report = std::fs::read_to_string(&report).unwrap_or_default();
    Outcome { status, report }
}
fn quiet() -> [Stdio; 3] {
    [Stdio::null(), Stdio::null(), Stdio::null()]
}
fn bootstrap_pipe(bytes: &[u8]) -> std::io::PipeReader {
    let (r, mut w) = std::io::pipe().unwrap();
    w.write_all(bytes).unwrap();
    drop(w);
    r
}
fn is_open(fd: i32) -> bool {
    // SAFETY: one `fcntl(F_GETFD)`, defined for any integer.
    fcntl_getfd(unsafe { BorrowedFd::borrow_raw(fd) }).is_ok()
}
fn closed_number() -> i32 {
    (900..1000)
        .rev()
        .find(|fd| !is_open(*fd))
        .expect("a free high descriptor number")
}

#[test]
fn passed_pipe_is_adopted_close_on_exec_and_readable() {
    let r = bootstrap_pipe(&SECRET);
    let outcome = run(r.as_raw_fd(), "fifo", "", &[r.as_raw_fd()], quiet());
    assert_eq!(outcome.status.code(), Some(0), "{}", outcome.report);
    assert!(outcome.has("adopted") && outcome.has("secret_ok=true") && outcome.has("bytes=32"));
    assert!(
        outcome.has("cloexec=true"),
        "adopted descriptor must be close-on-exec"
    );
    assert!(
        outcome.has("grandchild_inherited=false"),
        "a child of the sidecar must not receive the bootstrap descriptor: {}",
        outcome.report
    );
}
#[test]
fn closed_descriptor_number_is_refused_not_aborted() {
    let n = closed_number();
    let outcome = run(n, "fifo", "", &[], quiet());
    assert!(
        outcome.status.signal().is_none(),
        "v0.1.4 aborts here (134); v0.2 must not: {:?}",
        outcome.status
    );
    assert_ne!(outcome.status.code(), Some(134));
    assert!(
        outcome.refused_cleanly(),
        "{:?} {}",
        outcome.status,
        outcome.report
    );
    assert!(
        outcome.has("refused=descriptor_closed") && outcome.has("sidecar=invalid_bootstrap_fd")
    );
}
#[test]
fn stdio_and_negative_numbers_are_refused_even_when_they_are_valid_pipes() {
    // 0, 1 and 2 are real pipes here, so only the number rule can refuse them.
    for fd in [0, 1, 2] {
        let outcome = run(
            fd,
            "fifo",
            "",
            &[],
            [Stdio::piped(), Stdio::piped(), Stdio::piped()],
        );
        assert!(
            outcome.refused_cleanly(),
            "fd {fd}: {:?} {}",
            outcome.status,
            outcome.report
        );
        assert!(
            outcome.has("refused=descriptor_reserved"),
            "fd {fd}: {}",
            outcome.report
        );
        assert!(!outcome.has("adopted"));
    }
    let outcome = run(-1, "fifo", "", &[], quiet());
    assert!(
        outcome.refused_cleanly() && outcome.has("refused=descriptor_negative"),
        "{}",
        outcome.report
    );
    assert!(outcome.has("sidecar=invalid_bootstrap_fd"));
}
#[test]
fn wrong_kind_is_refused_and_a_non_pipe_maps_to_bootstrap_not_pipe() {
    let (socket, _peer) = std::os::unix::net::UnixStream::pair().unwrap();
    let regular = std::fs::File::open(env!("CARGO_MANIFEST_PATH")).unwrap();
    let null = std::fs::File::open("/dev/null").unwrap();
    let pipe = bootstrap_pipe(&SECRET);
    let cases: [(i32, &str, &str); 6] = [
        (socket.as_raw_fd(), "fifo", "socket"),
        (regular.as_raw_fd(), "fifo", "regular"),
        (null.as_raw_fd(), "fifo", "character device"),
        (pipe.as_raw_fd(), "socket", "pipe wanted as socket"),
        (pipe.as_raw_fd(), "regular", "pipe wanted as regular"),
        (
            null.as_raw_fd(),
            "regular",
            "character device wanted as regular",
        ),
    ];
    for (fd, wanted, what) in cases {
        let outcome = run(fd, wanted, "", &[fd], quiet());
        assert!(
            outcome.refused_cleanly(),
            "{what}: {:?} {}",
            outcome.status,
            outcome.report
        );
        assert!(
            outcome.has("refused=descriptor_wrong_kind"),
            "{what}: {}",
            outcome.report
        );
        assert!(!outcome.has("adopted"), "{what}");
    }
    let outcome = run(
        socket.as_raw_fd(),
        "fifo",
        "",
        &[socket.as_raw_fd()],
        quiet(),
    );
    assert!(
        outcome.has("sidecar=bootstrap_not_pipe"),
        "{}",
        outcome.report
    );
}
#[test]
fn other_expected_kinds_are_adopted_when_they_match() {
    let (socket, _peer) = std::os::unix::net::UnixStream::pair().unwrap();
    let regular = std::fs::File::open(env!("CARGO_MANIFEST_PATH")).unwrap();
    for (fd, kind) in [
        (socket.as_raw_fd(), "socket"),
        (regular.as_raw_fd(), "regular"),
    ] {
        let outcome = run(fd, kind, "", &[fd], quiet());
        assert_eq!(outcome.status.code(), Some(0), "{kind}: {}", outcome.report);
        assert!(
            outcome.has("adopted") && outcome.has("cloexec=true"),
            "{kind}: {}",
            outcome.report
        );
        assert!(
            outcome.has("grandchild_inherited=false"),
            "{kind}: {}",
            outcome.report
        );
    }
}
#[test]
fn the_same_number_is_adopted_once_per_process_even_after_it_is_closed() {
    let r = bootstrap_pipe(&SECRET);
    let fd = r.as_raw_fd();
    let outcome = run(fd, "fifo", "twice", &[fd], quiet());
    assert_eq!(outcome.status.code(), Some(0), "{}", outcome.report);
    assert!(outcome.has("adopted"));
    assert!(
        outcome.has("second=descriptor_already_adopted"),
        "{}",
        outcome.report
    );
    // After the owner dropped it the number is closed, and in any case never adoptable again.
    assert!(
        outcome.has("third=descriptor_closed") || outcome.has("third=descriptor_already_adopted"),
        "{}",
        outcome.report
    );
    assert!(!outcome.has("third=adopted"));
}
