// SPDX-License-Identifier: AGPL-3.0-only
// P:path:mycelium_node/process.py (Python `subprocess.Popen(..., close_fds=True, pass_fds=...)`:
// a child keeps exactly the listed descriptors and nothing else). L6 round 9: the same
// contract at the single audited spawn boundary, `confine_descriptors_except`, and through
// NativeChild / GroupChild / PersistentChild (guardian) `spawn_passing`. Real exec'd
// children; decoys are created concurrently by other threads while spawns are in flight.
#![allow(unsafe_code)]
mod support;
use control_runtime::Store;
use node::{
    WorkerAdapter, WorkerError,
    fds::{DescriptorKind, PassRefusal, adopt_inherited_descriptor, pass_refusal},
    guardian::{PersistentChild, WorkerCommand},
    process::NativeChild,
    recovery::GroupChild,
};
use rustix::{
    fs::{FileType, fstat},
    io::{Errno, FdFlags, fcntl_dupfd_cloexec, fcntl_getfd, fcntl_setfd},
    process::{Resource, Rlimit, getrlimit, setrlimit},
};
use std::{
    collections::VecDeque,
    io::{Read, Write},
    net::{TcpListener, UdpSocket},
    os::{
        fd::{AsFd, AsRawFd, BorrowedFd, OwnedFd, RawFd},
        unix::net::UnixStream,
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::{AtomicBool, AtomicU64, Ordering},
    time::Instant,
};
// Descriptor tables of this whole process are asserted: tests must not overlap.
static SERIAL: std::sync::Mutex<()> = std::sync::Mutex::new(());
static NEXT: AtomicU64 = AtomicU64::new(0);
fn serial() -> std::sync::MutexGuard<'static, ()> {
    SERIAL
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}
const SECRET: [u8; 32] = [0xa5; 32];
const ROUNDS: usize = 3;
const THREADS: usize = 2;
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l6-pass-{}-{}",
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
fn wait(mut condition: impl FnMut() -> bool) {
    let end = Instant::now() + support::bounded();
    while !condition() {
        assert!(Instant::now() < end, "fixture deadline");
        std::thread::sleep(support::poll());
    }
}
fn ticket() -> node::Ticket {
    node::Ticket {
        epoch: 1,
        run: 1,
        node: "owned".into(),
        incarnation: "pass".into(),
    }
}
fn stopped(worker: &mut dyn WorkerAdapter) {
    worker.request_stop().unwrap();
    wait(|| match worker.observe() {
        Ok(residue) => residue.process_gone && residue.resources_gone,
        Err(WorkerError::Io(e)) if e.kind() == std::io::ErrorKind::PermissionDenied => false,
        Err(e) => panic!("cleanup failed: {e:?}"),
    });
}

// ---- child side ---------------------------------------------------------------------

/// Worker fixture: report every open descriptor as `fd kind dev ino`; if `L6_PASS_READ_FD`
/// is set, adopt it as the bootstrap FIFO (S-L9-2) and read the 32-byte secret from it.
#[test]
fn pass_fixture() {
    let Some(report) = std::env::var_os("L6_PASS_REPORT") else {
        return;
    };
    support::require_anchor();
    let limit = getrlimit(Resource::Nofile).current.unwrap_or(4096);
    let mut lines = String::new();
    for fd in 0..i32::try_from(limit).unwrap_or(i32::MAX) {
        // SAFETY: one `fstat` borrow of an integer descriptor; nothing is closed or owned.
        let Ok(stat) = fstat(unsafe { BorrowedFd::borrow_raw(fd) }) else {
            continue;
        };
        let kind = kind_name(FileType::from_raw_mode(stat.st_mode));
        lines.push_str(&format!("{fd} {kind} {} {}\n", stat.st_dev, stat.st_ino));
    }
    if let Some(fd) = std::env::var("L6_PASS_READ_FD")
        .ok()
        .and_then(|v| v.parse::<i32>().ok())
    {
        // A descriptor that never arrived must still produce a report (and a failing
        // assertion in the parent), not a 30-second wait.
        let ok = adopt_inherited_descriptor(fd, DescriptorKind::Fifo).is_ok_and(|owned| {
            let mut bytes = [0u8; 32];
            std::fs::File::from(owned).read_exact(&mut bytes).is_ok() && bytes == SECRET
        });
        lines.push_str(&format!("secret_ok={ok}\n"));
    }
    let report = PathBuf::from(report);
    let partial = report.with_extension("partial");
    std::fs::write(&partial, lines).unwrap();
    std::fs::rename(&partial, &report).unwrap();
    support::hold()
}

// ---- parent side --------------------------------------------------------------------

#[derive(Debug, PartialEq, Eq)]
struct Entry {
    fd: RawFd,
    kind: String,
    dev: i128,
    ino: u64,
}
fn identity(fd: &impl AsFd) -> (i128, u64) {
    let stat = fstat(fd).unwrap();
    (i128::from(stat.st_dev), stat.st_ino)
}
fn kind_name(kind: FileType) -> &'static str {
    match kind {
        FileType::Fifo => "fifo",
        FileType::Socket => "socket",
        FileType::RegularFile => "file",
        FileType::CharacterDevice => "chr",
        _ => "other",
    }
}
fn kind_of(fd: &impl AsFd) -> &'static str {
    kind_name(FileType::from_raw_mode(fstat(fd).unwrap().st_mode))
}
fn entry(fd: &impl AsFd) -> Entry {
    let (dev, ino) = identity(fd);
    Entry {
        fd: fd.as_fd().as_raw_fd(),
        kind: kind_of(fd).into(),
        dev,
        ino,
    }
}
fn read_report(report: &Path) -> (Vec<Entry>, Option<bool>) {
    wait(|| report.exists());
    let text = std::fs::read_to_string(report).unwrap();
    let mut entries = Vec::new();
    let mut secret = None;
    for line in text.lines() {
        if let Some(value) = line.strip_prefix("secret_ok=") {
            secret = Some(value == "true");
            continue;
        }
        let mut parts = line.split(' ');
        entries.push(Entry {
            fd: parts.next().unwrap().parse().unwrap(),
            kind: parts.next().unwrap().into(),
            dev: parts.next().unwrap().parse().unwrap(),
            ino: parts.next().unwrap().parse().unwrap(),
        });
    }
    (entries, secret)
}
/// What the worker may hold: its three redirected stdio descriptors (`/dev/null`
/// character devices) and exactly `expected`, matched on descriptor number AND the open
/// file's device+inode — never a decoy that happens to have the same number.
fn assert_exact(report: &Path, expected: &[Entry]) -> Option<bool> {
    let (entries, secret) = read_report(report);
    let (stdio, rest): (Vec<_>, Vec<_>) = entries.iter().partition(|e| e.fd < 3);
    assert_eq!(
        stdio
            .iter()
            .map(|e| (e.fd, e.kind.as_str()))
            .collect::<Vec<_>>(),
        [(0, "chr"), (1, "chr"), (2, "chr")],
        "stdio redirected by Stdio settings"
    );
    let mut want: Vec<&Entry> = expected.iter().collect();
    want.sort_by_key(|e| e.fd);
    assert_eq!(
        rest, want,
        "worker must hold exactly the listed descriptors (decoys leaked or listed one lost)"
    );
    secret
}

/// Parent descriptors a careless/concurrent creator would leave inheritable: every kind,
/// both ends of pipes and socket pairs, close-on-exec cleared (the creation gap).
fn decoy_batch() -> Vec<OwnedFd> {
    let (pr, pw) = std::io::pipe().unwrap();
    let (a, b) = UnixStream::pair().unwrap();
    let fds: Vec<OwnedFd> = vec![
        pr.into(),
        pw.into(),
        a.into(),
        b.into(),
        UdpSocket::bind("127.0.0.1:0").unwrap().into(),
        TcpListener::bind("127.0.0.1:0").unwrap().into(),
        std::fs::File::open("/dev/zero").unwrap().into(),
        std::fs::File::open("/dev/null").unwrap().into(),
    ];
    for fd in &fds {
        fcntl_setfd(fd, FdFlags::empty()).unwrap();
    }
    fds
}
/// Threads that create and drop decoys the whole time spawns are in flight.
fn churn<R>(body: impl FnOnce() -> R) -> R {
    let stop = AtomicBool::new(false);
    std::thread::scope(|scope| {
        let creators: Vec<_> = (0..THREADS)
            .map(|_| {
                scope.spawn(|| {
                    let mut live: VecDeque<Vec<OwnedFd>> = VecDeque::new();
                    while !stop.load(Ordering::SeqCst) {
                        live.push_back(decoy_batch());
                        if live.len() > 2 {
                            live.pop_front();
                        }
                    }
                })
            })
            .collect();
        // A failing assertion in `body` must still stop the creators, or the scope hangs.
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(body));
        stop.store(true, Ordering::SeqCst);
        for creator in creators {
            creator.join().unwrap();
        }
        result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
    })
}

#[derive(Clone, Copy, Debug)]
enum Variant {
    Native,
    Group,
    Guardian,
}
const VARIANTS: [Variant; 3] = [Variant::Native, Variant::Group, Variant::Guardian];
fn fixture_arguments() -> Vec<std::ffi::OsString> {
    ["--exact", "pass_fixture", "--nocapture"]
        .map(Into::into)
        .into()
}
fn fixture_environment(report: &Path, read: Option<RawFd>) -> Vec<(String, std::ffi::OsString)> {
    let mut env = vec![
        ("L6_PASS_REPORT".to_string(), report.as_os_str().into()),
        (support::ANCHOR.to_string(), support::anchor_value()),
    ];
    if let Some(fd) = read {
        env.push(("L6_PASS_READ_FD".to_string(), fd.to_string().into()));
    }
    env
}
fn fixture_command(report: &Path, read: Option<RawFd>) -> Command {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command.args(fixture_arguments());
    for (k, v) in fixture_environment(report, read) {
        command.env(k, v);
    }
    command
}
/// Start the fixture under `variant`, passing `pass` through that variant's builder.
fn start(
    variant: Variant,
    base: &Path,
    n: usize,
    pass: &[RawFd],
    read: Option<RawFd>,
) -> Result<(Box<dyn WorkerAdapter>, PathBuf), WorkerError> {
    let report = base.join(format!("report-{n}"));
    let worker: Box<dyn WorkerAdapter> = match variant {
        Variant::Native => {
            let child = NativeChild::spawn_passing(
                &mut fixture_command(&report, read),
                &base.join(format!("native-{n}")),
                pass,
            )?;
            let pid = child.pid();
            Box::new(support::Reaped::new(child, vec![pid]))
        }
        Variant::Group => {
            let mut store = Store::open(&base.join(format!("group-{n}.db"))).unwrap();
            let child = GroupChild::spawn_passing(
                &mut fixture_command(&report, read),
                &base.join(format!("group-{n}")),
                &mut store,
                "group",
                ticket(),
                pass,
            )?;
            let pid = child.pid();
            Box::new(support::Reaped::new(child, vec![pid]))
        }
        Variant::Guardian => {
            let worker = WorkerCommand {
                program: std::env::current_exe().unwrap(),
                arguments: fixture_arguments(),
                environment: fixture_environment(&report, read)
                    .into_iter()
                    .map(|(k, v)| (k.into(), Some(v)))
                    .collect(),
            };
            let child = PersistentChild::spawn_passing(
                Path::new(env!("CARGO_BIN_EXE_node-guardian")),
                &worker,
                &base.join(format!("guarded-{n}")),
                &base.join(format!("guarded-{n}.db")),
                "guarded",
                ticket(),
                pass,
            )?;
            let (guardian, worker) = child.process_ids().unwrap();
            Box::new(support::Reaped::new(child, vec![guardian, worker]))
        }
    };
    Ok((worker, report))
}

#[test]
fn exactly_the_listed_pipe_arrives_amid_concurrently_created_decoys() {
    let _serial = serial();
    for variant in VARIANTS {
        let t = Temp::new();
        let _static_decoys = decoy_batch();
        churn(|| {
            for round in 0..ROUNDS {
                let (reader, mut writer) = std::io::pipe().unwrap();
                writer.write_all(&SECRET).unwrap();
                let fd = reader.as_raw_fd();
                assert!(
                    fcntl_getfd(&reader).unwrap().contains(FdFlags::CLOEXEC),
                    "a normally created pipe is close-on-exec in the parent"
                );
                let (mut worker, report) = start(variant, &t.0, round, &[fd], Some(fd)).unwrap();
                let secret = assert_exact(&report, &[entry(&reader)]);
                assert_eq!(
                    secret,
                    Some(true),
                    "{variant:?}: adopted pipe carried the secret"
                );
                stopped(&mut *worker);
                // The parent's own descriptor is unharmed and still close-on-exec.
                assert!(fcntl_getfd(&reader).unwrap().contains(FdFlags::CLOEXEC));
            }
        });
    }
}

/// Round 10 (enumeration instead of a scan to the limit): listed descriptors and decoys far
/// above the dense low range, with decoys still being created concurrently. Matched on
/// number AND device+inode, so a decoy with a listed number cannot pass.
#[test]
fn listed_and_decoy_descriptors_far_above_the_low_range_are_handled_exactly() {
    let _serial = serial();
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
    for variant in VARIANTS {
        let t = Temp::new();
        let mut high_decoys = Vec::new();
        for min in [300, 900, 2000] {
            let source = std::fs::File::open("/dev/zero").unwrap();
            let fd = fcntl_dupfd_cloexec(&source, min).unwrap();
            fcntl_setfd(&fd, FdFlags::empty()).unwrap();
            high_decoys.push(fd);
        }
        churn(|| {
            let (reader, mut writer) = std::io::pipe().unwrap();
            writer.write_all(&SECRET).unwrap();
            let listed_low = reader.as_raw_fd();
            let (peer, _other) = UnixStream::pair().unwrap();
            let listed_high = fcntl_dupfd_cloexec(&peer, 1200).unwrap();
            assert!(listed_high.as_raw_fd() >= 1200);
            let list = [listed_low, listed_high.as_raw_fd()];
            let (mut worker, report) = start(variant, &t.0, 0, &list, Some(listed_low)).unwrap();
            let secret = assert_exact(&report, &[entry(&reader), entry(&listed_high)]);
            assert_eq!(
                secret,
                Some(true),
                "{variant:?}: adopted pipe carried the secret"
            );
            stopped(&mut *worker);
            assert!(
                fcntl_getfd(&listed_high)
                    .unwrap()
                    .contains(FdFlags::CLOEXEC)
            );
        });
    }
}

#[test]
fn an_empty_list_is_exactly_confine_descriptors() {
    let _serial = serial();
    for variant in VARIANTS {
        let t = Temp::new();
        let _static_decoys = decoy_batch();
        churn(|| {
            for round in 0..2 {
                let (mut worker, report) = start(variant, &t.0, round, &[], None).unwrap();
                assert_exact(&report, &[]);
                stopped(&mut *worker);
            }
        });
    }
}

#[test]
fn several_listed_descriptors_of_different_kinds_all_arrive_and_only_they_do() {
    let _serial = serial();
    let t = Temp::new();
    let _decoys = decoy_batch();
    let (reader, _writer) = std::io::pipe().unwrap();
    let (socket, _peer) = UnixStream::pair().unwrap();
    let file = std::fs::File::open("/dev/zero").unwrap();
    // One close-on-exec (normal), one in the creation gap, one close-on-exec again.
    fcntl_setfd(&socket, FdFlags::empty()).unwrap();
    let list = [reader.as_raw_fd(), socket.as_raw_fd(), file.as_raw_fd()];
    let report = t.0.join("report");
    let mut command = fixture_command(&report, None);
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    node::fds::confine_descriptors_except(&mut command, &list).unwrap();
    let child = support::Owned(command.spawn().unwrap());
    let expected = [entry(&reader), entry(&socket), entry(&file)];
    let result = std::panic::catch_unwind(|| assert_exact(&report, &expected));
    drop(child);
    result.unwrap();
}

#[test]
fn the_parent_flags_are_never_changed_only_the_childs() {
    let _serial = serial();
    let t = Temp::new();
    let (closed_on_exec, _w) = std::io::pipe().unwrap();
    let (gap, _peer) = UnixStream::pair().unwrap();
    fcntl_setfd(&gap, FdFlags::empty()).unwrap();
    let before = [
        fcntl_getfd(&closed_on_exec).unwrap(),
        fcntl_getfd(&gap).unwrap(),
    ];
    assert!(before[0].contains(FdFlags::CLOEXEC) && before[1].is_empty());
    let report = t.0.join("report");
    let mut command = fixture_command(&report, None);
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    node::fds::confine_descriptors_except(
        &mut command,
        &[closed_on_exec.as_raw_fd(), gap.as_raw_fd()],
    )
    .unwrap();
    // Registering the keep-list must not touch the parent (no clear before the fork).
    let registered = [
        fcntl_getfd(&closed_on_exec).unwrap(),
        fcntl_getfd(&gap).unwrap(),
    ];
    assert_eq!(registered, before, "flags changed at registration time");
    let child = support::Owned(command.spawn().unwrap());
    let seen = std::panic::catch_unwind(|| read_report(&report));
    let after = [
        fcntl_getfd(&closed_on_exec).unwrap(),
        fcntl_getfd(&gap).unwrap(),
    ];
    drop(child);
    let (entries, _) = seen.unwrap();
    assert_eq!(after, before, "flags changed in the parent by the spawn");
    assert_eq!(
        entries.iter().filter(|e| e.fd >= 3).count(),
        2,
        "the child received both"
    );
}

#[test]
fn a_listed_descriptor_closed_after_validation_fails_the_spawn_in_the_child() {
    let _serial = serial();
    // std opens its exec-error pipe (lowest free numbers) before the fork: free two lower
    // numbers first so the closed listed number is not immediately reused by std itself.
    let spare = std::io::pipe().unwrap();
    let (reader, _writer) = std::io::pipe().unwrap();
    let fd = reader.as_raw_fd();
    drop(spare);
    let mut command = Command::new("/usr/bin/true");
    node::fds::confine_descriptors_except(&mut command, &[fd]).unwrap();
    drop(reader); // closed between validation and fork
    let error = command
        .spawn()
        .expect_err("must not exec without the descriptor");
    assert_eq!(error.raw_os_error(), Some(Errno::BADF.raw_os_error()));
}

#[test]
fn exec_failure_is_still_reported_with_a_keep_list() {
    let _serial = serial();
    let (reader, _writer) = std::io::pipe().unwrap();
    let mut command = Command::new("/nonexistent/v02-l6-no-such-program");
    node::fds::confine_descriptors_except(&mut command, &[reader.as_raw_fd()]).unwrap();
    let error = command.spawn().expect_err("missing executable");
    assert_eq!(error.kind(), std::io::ErrorKind::NotFound);
}

#[test]
fn stdio_negative_closed_and_duplicate_are_refused_before_anything_is_registered() {
    let _serial = serial();
    let (reader, _writer) = std::io::pipe().unwrap();
    let ok = reader.as_raw_fd();
    let mut command = Command::new("/usr/bin/true");
    let mut refusal = |list: &[RawFd]| {
        let error =
            node::fds::confine_descriptors_except(&mut command, list).expect_err("must be refused");
        pass_refusal(&error).expect("carries its reason")
    };
    for stdio in 0..=2 {
        assert_eq!(refusal(&[stdio]), PassRefusal::Reserved, "{stdio}");
        assert_eq!(
            refusal(&[ok, stdio]),
            PassRefusal::Reserved,
            "{stdio} after a good one"
        );
    }
    assert_eq!(refusal(&[-1]), PassRefusal::Negative);
    assert_eq!(refusal(&[ok, i32::MIN]), PassRefusal::Negative);
    let closed = {
        let (gone, _w) = std::io::pipe().unwrap();
        gone.as_raw_fd()
    };
    assert_eq!(refusal(&[closed]), PassRefusal::Closed);
    assert_eq!(refusal(&[ok, closed]), PassRefusal::Closed);
    assert_eq!(refusal(&[ok, ok]), PassRefusal::Duplicate);
    assert_eq!(refusal(&[ok, 1_000_000]), PassRefusal::Closed);
    // Nothing was registered by any refusal: the command still runs and the pipe is not
    // kept (an accepted list would have cleared its flag in the child; here `true` has no
    // way to say, but a stale registration would have failed the spawn for the closed one).
    let status = command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .unwrap();
    assert!(status.success());
}

#[test]
fn a_refused_list_leaves_no_resource_behind_in_any_adapter() {
    let _serial = serial();
    let t = Temp::new();
    for (n, variant) in VARIANTS.into_iter().enumerate() {
        let Err(error) = start(variant, &t.0, n, &[1], None) else {
            panic!("{variant:?}: stdio in the keep-list must refuse the spawn");
        };
        let WorkerError::Io(io) = error else {
            panic!("{variant:?}: refusal not surfaced as the io error: {error:?}");
        };
        assert_eq!(
            pass_refusal(&io),
            Some(PassRefusal::Reserved),
            "{variant:?}"
        );
    }
    // The group variant's store is opened by the test helper before the adapter is asked.
    let names: Vec<String> = std::fs::read_dir(&t.0)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| !name.starts_with("group-1.db"))
        .collect();
    assert!(
        names.is_empty(),
        "refusal created resources/journals/directories: {names:?}"
    );
}
#[test]
fn a_refused_list_writes_no_group_journal() {
    let _serial = serial();
    let t = Temp::new();
    assert!(start(Variant::Group, &t.0, 1, &[2], None).is_err());
    let store = Store::open(&t.0.join("group-1.db")).unwrap();
    assert!(store.load_document("group").unwrap().is_none());
}
