// SPDX-License-Identifier: AGPL-3.0-only
//! Lane MG item 2: the worker memory guard. (a) Deterministic: readings are
//! injected through the constructor-only probe seam, one rendezvous per reading,
//! so "reading k was judged" is proven by the guard taking reading k+1; real
//! owned children (`/bin/cat`, `tail`) are killed and reaped. (b) One real
//! process that allocates and touches memory above a small cap.
use super::super::{Worker, duration};
use super::*;
use crate::native_compiler::{Config, file_pin};
use budgets::BudgetId;
use std::{
    io::{BufRead, BufReader},
    os::{fd::OwnedFd, unix::net::UnixStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc,
        atomic::AtomicBool,
        mpsc::{Receiver, SyncSender, sync_channel},
    },
    time::{Duration, Instant},
};

const LIMIT: u64 = 1 << 30;
type Reading = std::result::Result<u64, &'static str>;

/// Hands the guard exactly the readings the test sends. A closed script reads
/// as an idle worker, so test teardown can never trip the guard by itself.
struct Scripted(Receiver<Reading>);
impl MemoryProbe for Scripted {
    fn resident_bytes(&mut self, _pid: u32) -> Reading {
        self.0.recv().unwrap_or(Ok(0))
    }
}
fn flag() -> Arc<AtomicBool> {
    Arc::new(AtomicBool::new(false))
}
/// An owned child holding the worker socket's far end, idle until killed.
fn idle_child() -> (Child, UnixStream) {
    let (client, server) = UnixStream::pair().unwrap();
    let child = Command::new("/bin/cat")
        .stdin(Stdio::piped())
        .stdout(Stdio::from(OwnedFd::from(server)))
        .spawn()
        .unwrap();
    (child, client)
}
fn guarded(child: Child, client: UnixStream) -> (Worker, std::sync::mpsc::SyncSender<Reading>) {
    let (readings, script) = sync_channel(0);
    let worker =
        Worker::from_child_guarded(child, client, flag(), LIMIT, Box::new(Scripted(script)))
            .unwrap();
    (worker, readings)
}
/// Reaped (waitpid: no such child), not running (kill 0: no such process) and
/// absent from `ps`.
fn assert_gone(pid: u32) {
    let raw = rustix::process::Pid::from_raw(pid as i32).unwrap();
    assert_eq!(
        rustix::process::waitpid(Some(raw), rustix::process::WaitOptions::NOHANG)
            .map(|status| status.is_some()),
        Err(rustix::io::Errno::CHILD),
        "the worker was not reaped"
    );
    assert_eq!(
        rustix::process::test_kill_process(raw),
        Err(rustix::io::Errno::SRCH)
    );
    let ps = Command::new("/bin/ps")
        .args(["-o", "pid=", "-p", &pid.to_string()])
        .output()
        .unwrap();
    assert!(
        String::from_utf8_lossy(&ps.stdout).trim().is_empty(),
        "ps still lists {pid}"
    );
}
fn step(worker: &mut Worker) -> std::result::Result<(), &'static str> {
    let deadline = Instant::now() + duration(BudgetId::ModelSelection).unwrap();
    worker
        .request_until(&json!({"command":"step"}), &[], deadline)
        .map(|_| ())
        .map_err(|e| e.0)
}

#[test]
fn readings_at_or_under_the_cap_leave_the_worker_running() {
    let (child, client) = idle_child();
    let pid = child.id();
    let (worker, readings) = guarded(child, client);
    for bytes in [0, LIMIT / 2, LIMIT, LIMIT] {
        assert!(
            readings.send(Ok(bytes)).is_ok(),
            "the guard must take every reading"
        );
    }
    // The last rendezvous proves the first three readings were judged.
    assert!(worker.guard_samples() >= 3);
    assert_eq!(worker.guard_failure(), None);
    assert_eq!(
        rustix::process::test_kill_process(rustix::process::Pid::from_raw(pid as i32).unwrap()),
        Ok(())
    );
    drop(readings);
    let evidence = worker.kill_and_reap().unwrap();
    assert_eq!(evidence["killed"], true);
    assert_gone(pid);
}

#[test]
fn the_first_reading_over_the_cap_kills_reaps_and_reports_the_numbers() {
    let (child, client) = idle_child();
    let pid = child.id();
    let (mut worker, readings) = guarded(child, client);
    for bytes in [LIMIT / 2, LIMIT + 7] {
        assert!(
            readings.send(Ok(bytes)).is_ok(),
            "the guard must take every reading"
        );
    }
    // The running (or next) request fails with the guard's code.
    assert_eq!(step(&mut worker), Err(MEMORY_LIMIT_EXCEEDED));
    assert_eq!(
        worker.guard_failure(),
        Some(Failure {
            code: MEMORY_LIMIT_EXCEEDED,
            limit_detail: json!({"observed_group_rss_bytes":LIMIT + 7,"max_rss_bytes":LIMIT,"overshoot_bytes":7}),
            samples: 2,
        })
    );
    assert_gone(pid);
    // Every later command answers the same code; the stage can still be released.
    assert_eq!(step(&mut worker), Err(MEMORY_LIMIT_EXCEEDED));
    assert_eq!(worker.kill_and_reap().unwrap()["worker_reaped"], true);
    assert_gone(pid);
}

#[test]
fn a_failed_reading_fails_closed() {
    let (child, client) = idle_child();
    let pid = child.id();
    let (mut worker, readings) = guarded(child, client);
    assert!(
        readings.send(Err("probe_unavailable")).is_ok(),
        "the guard must take every reading"
    );
    assert_eq!(step(&mut worker), Err(MEMORY_OBSERVATION_FAILED));
    assert_eq!(
        worker.guard_failure(),
        Some(Failure {
            code: MEMORY_OBSERVATION_FAILED,
            limit_detail: Value::Null,
            samples: 1,
        })
    );
    assert_gone(pid);
    assert_eq!(worker.kill_and_reap().unwrap()["worker_reaped"], true);
}

/// The member start path (`start_scoped_until`, reached from `NativeShard`)
/// guards the worker from spawn on, at the prepare document's `memory`: a
/// reading equal to it is allowed, one byte more stops the start by name.
#[test]
fn the_member_start_path_guards_the_worker_at_the_prepared_memory() {
    let root = std::env::temp_dir().join(format!(
        "mg-start-{}-{}",
        std::process::id(),
        crate::app_host::hex(&crate::app_host::random(8).unwrap())
    ));
    std::fs::create_dir(&root).unwrap();
    let root = std::fs::canonicalize(root).unwrap();
    let binary = root.join("worker.sh");
    std::fs::write(&binary, "#!/bin/sh\nexec /usr/bin/tail -f /dev/null\n").unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&binary, std::fs::Permissions::from_mode(0o700)).unwrap();
    let config = Config {
        pin: file_pin(&binary).unwrap(),
        binary,
    };
    let prepare = json!({"command":stage_contract::native_shard::COMMAND,
        "checkpoint":root.join("shard.safetensors"),"memory":LIMIT});
    let (readings, script) = sync_channel(0);
    let feeder = std::thread::spawn(move || {
        [LIMIT, LIMIT + 1]
            .into_iter()
            .all(|bytes| readings.send(Ok(bytes)).is_ok())
    });
    let started = Worker::start_scoped_probed_until(
        &config,
        &prepare,
        flag(),
        &root.join("source"),
        Instant::now() + duration(BudgetId::ModelSelection).unwrap(),
        None,
        Box::new(Scripted(script)),
    );
    assert!(feeder.join().unwrap(), "the guard must take both readings");
    assert_eq!(
        started.as_ref().map(|_| ()).map_err(|e| e.error.0),
        Err(MEMORY_LIMIT_EXCEEDED)
    );
    // MGF F4: the failed start hands back the guard's numbers.
    assert_eq!(
        started.err().and_then(|e| e.guard).map(|f| f.limit_detail),
        Some(
            json!({"observed_group_rss_bytes":LIMIT + 1,"max_rss_bytes":LIMIT,"overshoot_bytes":1})
        )
    );
    std::fs::remove_dir_all(&root).unwrap();
}

const REAL_LIMIT: u64 = 96 << 20;
const HOG_BYTES: usize = 320 << 20;

#[test]
#[ignore = "subprocess fixture, invoked only by the real-memory guard test"]
fn memory_hog_process() {
    let bytes: usize = std::env::var("MG_HOG_BYTES").unwrap().parse().unwrap();
    let mut hog = vec![0_u8; bytes];
    for page in hog.chunks_mut(4096) {
        page[0] = 1;
    }
    std::hint::black_box(&mut hog);
    eprintln!("mg_hog_resident");
    loop {
        std::thread::park();
    }
}

/// (b) One real child allocates and touches memory above a small cap; the
/// host probe sees it and the guard kills and reaps it: while it is still
/// touching (any reading above the cap counts), or within two samples of its
/// memory being resident. No model, no Metal.
///
/// Bound (MGF F7): the verdict must arrive within v0.1.4's worker start
/// allowance (`WorkerStartup`, worker.py:36), generous for spawning this test
/// binary and touching 320 MiB under load; a guard that never kills fails at
/// that deadline. The sample bound counts readings, not time.
#[test]
fn a_real_child_over_the_cap_is_killed_and_reaped() {
    let (client, server) = UnixStream::pair().unwrap();
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--ignored",
            "--exact",
            "native_compiler::worker::guard::tests::memory_hog_process",
            "--nocapture",
        ])
        .env("MG_HOG_BYTES", HOG_BYTES.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::from(OwnedFd::from(server)))
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let pid = child.id();
    let stderr = child.stderr.take().unwrap();
    let worker =
        Worker::from_child_guarded(child, client, flag(), REAL_LIMIT, Box::new(HostProbe)).unwrap();
    let resident = BufReader::new(stderr)
        .lines()
        .map_while(std::result::Result::ok)
        .any(|line| line == "mg_hog_resident");
    let base = worker.guard_samples();
    let failure =
        worker.guard_wait_until(Instant::now() + duration(BudgetId::WorkerStartup).unwrap());
    assert_eq!(
        failure.as_ref().map(|f| f.code),
        Some(MEMORY_LIMIT_EXCEEDED),
        "resident marker seen: {resident}"
    );
    let failure = failure.unwrap();
    // A reading taken after the marker sees all of it: at most one sample
    // already counted at `base` read earlier. A child killed while still
    // touching never printed the marker (the reading above the cap is checked
    // below); the review's F7 flake was this case.
    if resident {
        assert!(
            failure.samples <= base + 2,
            "{} samples after the memory was resident at sample {base}",
            failure.samples
        );
    }
    let observed = failure.limit_detail["observed_group_rss_bytes"]
        .as_u64()
        .unwrap();
    assert!(observed > REAL_LIMIT);
    assert_eq!(failure.limit_detail["max_rss_bytes"], REAL_LIMIT);
    assert_eq!(
        failure.limit_detail["overshoot_bytes"],
        observed - REAL_LIMIT
    );
    assert_gone(pid);
    assert_eq!(worker.kill_and_reap().unwrap()["worker_reaped"], true);
}

/// Lane MG item 4 RED (un-ignored by lane MG2). v0.1.4
/// walks the worker's artifact root every 0.25 s and stops the worker with
/// `worker_disk_limit_exceeded` once its regular files exceed `max_disk_bytes`
/// (worker.py:820-844). v0.2 checked the shard size only once, at admission.
#[test]
fn a_custody_root_grown_over_the_storage_cap_stops_the_worker() {
    let root = std::env::temp_dir().join(format!(
        "mg-disk-{}-{}",
        std::process::id(),
        crate::app_host::hex(&crate::app_host::random(8).unwrap())
    ));
    std::fs::create_dir(&root).unwrap();
    let (child, client) = idle_child();
    let pid = child.id();
    let (mut worker, readings) = guarded(child, client);
    worker.guard_disk(&root, 1024).unwrap();
    std::fs::write(root.join("grown.bin"), vec![0_u8; 2048]).unwrap();
    for bytes in [LIMIT / 2, LIMIT / 2] {
        assert!(readings.send(Ok(bytes)).is_ok() || worker.guard_failure().is_some());
    }
    drop(readings);
    let failure =
        worker.guard_wait_until(Instant::now() + duration(BudgetId::ModelSelection).unwrap());
    std::fs::remove_dir_all(&root).unwrap();
    assert_eq!(failure.map(|f| f.code), Some("worker_disk_limit_exceeded"));
    assert_gone(pid);
}

// ---- Lane MG2 item 1: the storage walk (v0.1.4 `ManagedWorker._check`,
// worker.py:820-844 @ f4e366e9). The staging folder is walked every sample
// BEFORE the memory reading, so the guard taking reading k+1 proves the walk
// of sample k was judged; a verdict on the walk takes no reading at all.

fn temp_root(tag: &str) -> PathBuf {
    let root = std::env::temp_dir().join(format!(
        "mg2-{tag}-{}-{}",
        std::process::id(),
        crate::app_host::hex(&crate::app_host::random(8).unwrap())
    ));
    std::fs::create_dir(&root).unwrap();
    std::fs::canonicalize(root).unwrap()
}
/// Real sampling periods, but a clock that never advances: a walk can never
/// overrun its time budget, so the entry and depth bounds are tested alone
/// (the time budget has its own test with a ticking clock).
struct Frozen;
impl Pace for Frozen {
    fn period(&mut self, period: Duration, rest: &mut dyn FnMut(Duration) -> bool) -> bool {
        rest(period)
    }
    fn now(&mut self) -> Duration {
        Duration::ZERO
    }
}
/// An idle owned child guarded from construction on with `disk` (the member
/// start path's order), its memory readings scripted.
fn disk_guarded(disk: Disk) -> (Worker, SyncSender<Reading>, u32) {
    let (child, client) = idle_child();
    let pid = child.id();
    let (readings, script) = sync_channel(0);
    let worker = Worker::from_child_paced(
        child,
        client,
        flag(),
        LIMIT,
        Some(disk),
        Box::new(Scripted(script)),
        Box::new(Frozen),
    )
    .unwrap();
    (worker, readings, pid)
}
/// The guard's verdict on the folder `root` held to `max` (opened now), with
/// idle memory readings; the worker is checked gone after a verdict.
fn storage_verdict(root: &Path, max: u64) -> Option<Failure> {
    storage_verdict_of(Disk::open(root, max).unwrap())
}
fn storage_verdict_of(disk: Disk) -> Option<Failure> {
    let (worker, readings, pid) = disk_guarded(disk);
    // A verdict on the walk ends the guard: the next reading is never taken.
    for _ in 0..3 {
        if readings.send(Ok(0)).is_err() {
            break;
        }
    }
    drop(readings);
    let failure =
        worker.guard_wait_until(Instant::now() + duration(BudgetId::ModelSelection).unwrap());
    if failure.is_some() {
        assert_gone(pid);
    }
    assert_eq!(worker.kill_and_reap().unwrap()["worker_reaped"], true);
    failure
}
fn write(path: &Path, bytes: usize) {
    std::fs::write(path, vec![0_u8; bytes]).unwrap();
}

/// At the cap (regular files in the folder and below it, summed) nothing
/// happens: v0.1.4 refuses only `total > max_disk_bytes` (worker.py:841).
#[test]
fn storage_at_or_under_the_cap_leaves_the_worker_running() {
    let root = temp_root("under");
    write(&root.join("shard.safetensors"), 1000);
    std::fs::create_dir(root.join("nested")).unwrap();
    write(&root.join("nested").join("part"), 24);
    let (worker, readings, pid) = disk_guarded(Disk::open(&root, 1024).unwrap());
    for _ in 0..4 {
        assert!(
            readings.send(Ok(0)).is_ok(),
            "the guard must take every reading"
        );
    }
    assert!(worker.guard_samples() >= 3);
    assert_eq!(worker.guard_failure(), None);
    assert_eq!(
        rustix::process::test_kill_process(rustix::process::Pid::from_raw(pid as i32).unwrap()),
        Ok(())
    );
    drop(readings);
    assert_eq!(worker.kill_and_reap().unwrap()["killed"], true);
    assert_gone(pid);
    std::fs::remove_dir_all(&root).unwrap();
}

/// Over the cap: the first walk stops the worker by name, kills and reaps it,
/// and reports the three numbers as the memory guard does (v0.1.4 records
/// none for disk: additive, lane MG2).
#[test]
fn storage_over_the_cap_kills_reaps_and_reports_the_numbers() {
    let root = temp_root("over");
    write(&root.join("shard.safetensors"), 1000);
    std::fs::create_dir(root.join("nested")).unwrap();
    write(&root.join("nested").join("part"), 25);
    let (mut worker, readings, pid) = disk_guarded(Disk::open(&root, 1024).unwrap());
    let taken = readings.send(Ok(0)).is_ok();
    let failure =
        worker.guard_wait_until(Instant::now() + duration(BudgetId::ModelSelection).unwrap());
    assert!(!taken, "the walk is judged before the memory reading");
    assert_eq!(
        failure,
        Some(Failure {
            code: DISK_LIMIT_EXCEEDED,
            limit_detail: json!({"observed_disk_bytes":1025,"max_disk_bytes":1024,"overshoot_bytes":1}),
            samples: 1,
        })
    );
    assert_gone(pid);
    assert_eq!(step(&mut worker), Err(DISK_LIMIT_EXCEEDED));
    assert_eq!(worker.kill_and_reap().unwrap()["worker_reaped"], true);
    std::fs::remove_dir_all(&root).unwrap();
}

/// A folder the guard cannot observe stops the worker (fail closed), as
/// v0.1.4: root swapped or gone (identity kept at start, worker.py:705-709,
/// 823-828), unreadable, a non-regular entry (840), more entries than the
/// budgets row (835), or deeper than the walk's depth bound.
#[test]
fn an_unobservable_storage_root_fails_closed() {
    let closed = Some(STORAGE_OBSERVATION_FAILED);
    let code = |f: Option<Failure>| {
        f.map(|f| {
            assert_eq!(f.limit_detail, Value::Null);
            f.code
        })
    };
    // Swapped after the guard took its identity.
    let root = temp_root("swapped");
    let disk = Disk::open(&root, 1024).unwrap();
    let moved = root.with_extension("moved");
    std::fs::rename(&root, &moved).unwrap();
    std::fs::create_dir(&root).unwrap();
    assert_eq!(code(storage_verdict_of(disk)), closed, "swapped root");
    std::fs::remove_dir_all(&root).unwrap();
    std::fs::remove_dir_all(&moved).unwrap();
    // Gone.
    let root = temp_root("gone");
    let disk = Disk::open(&root, 1024).unwrap();
    std::fs::remove_dir(&root).unwrap();
    assert_eq!(code(storage_verdict_of(disk)), closed, "removed root");
    // Unreadable.
    use std::os::unix::fs::PermissionsExt;
    let root = temp_root("unreadable");
    std::fs::create_dir(root.join("locked")).unwrap();
    std::fs::set_permissions(root.join("locked"), std::fs::Permissions::from_mode(0o000)).unwrap();
    let verdict = storage_verdict(&root, 1024);
    std::fs::set_permissions(root.join("locked"), std::fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(code(verdict), closed, "unreadable directory");
    std::fs::remove_dir_all(&root).unwrap();
    // A socket: neither a regular file nor a directory.
    let root = temp_root("socket");
    let _listener = std::os::unix::net::UnixListener::bind(root.join("s")).unwrap();
    assert_eq!(code(storage_verdict(&root, 1024)), closed, "socket entry");
    std::fs::remove_dir_all(&root).unwrap();
    // One entry more than the budgets row allows (empty files: no size).
    let cap = BudgetId::PyMyceliumA13MacosWorkerManagedworkerCheckCountL835
        .count()
        .unwrap()
        .get();
    let root = temp_root("entries");
    for i in 0..=cap {
        std::fs::File::create(root.join(i.to_string())).unwrap();
    }
    assert_eq!(code(storage_verdict(&root, 1024)), closed, "entry count");
    std::fs::remove_dir_all(&root).unwrap();
    // One level deeper than the walk descends.
    let root = temp_root("deep");
    let mut deep = root.clone();
    for _ in 0..=WALK_DEPTH {
        deep = deep.join("d");
    }
    std::fs::create_dir_all(&deep).unwrap();
    assert_eq!(code(storage_verdict(&root, 1024)), closed, "depth");
    std::fs::remove_dir_all(&root).unwrap();
    // At the depth bound the walk completes.
    let root = temp_root("depth-bound");
    let mut deep = root.clone();
    for _ in 0..WALK_DEPTH {
        deep = deep.join("d");
    }
    std::fs::create_dir_all(&deep).unwrap();
    assert_eq!(
        code(storage_verdict(&root, 1024)),
        None,
        "at the depth bound"
    );
    std::fs::remove_dir_all(&root).unwrap();
}

/// A symlink in the folder is neither followed nor counted: v0.1.4 lstats
/// every entry and fails closed on a link (worker.py:829-836), so a link to a
/// large tree outside the folder is `worker_storage_observation_failed`, never
/// `worker_disk_limit_exceeded` (which following it would report).
#[test]
fn a_symlink_to_a_large_outside_tree_is_neither_followed_nor_counted() {
    let outside = temp_root("outside");
    std::fs::create_dir(outside.join("tree")).unwrap();
    write(&outside.join("tree").join("big"), 4096);
    for (name, target) in [
        ("dir", outside.join("tree")),
        ("file", outside.join("tree").join("big")),
    ] {
        let root = temp_root(name);
        write(&root.join("shard.safetensors"), 10);
        std::os::unix::fs::symlink(&target, root.join("escape")).unwrap();
        let verdict = storage_verdict(&root, 1024);
        assert_eq!(
            verdict.map(|f| (f.code, f.limit_detail)),
            Some((STORAGE_OBSERVATION_FAILED, Value::Null)),
            "a symlink to the outside {name}"
        );
        std::fs::remove_dir_all(&root).unwrap();
    }
    // The outside tree is untouched.
    assert_eq!(
        std::fs::metadata(outside.join("tree").join("big"))
            .unwrap()
            .len(),
        4096
    );
    std::fs::remove_dir_all(&outside).unwrap();
}

/// The folder must be a real directory when the guard starts (v0.1.4
/// `worker_artifact_root_invalid`, worker.py:705-707).
#[test]
fn the_storage_root_must_be_a_real_directory() {
    let root = temp_root("root-kind");
    write(&root.join("file"), 1);
    std::os::unix::fs::symlink(&root, root.join("link")).unwrap();
    for path in [root.join("file"), root.join("link"), root.join("absent")] {
        assert_eq!(
            Disk::open(&path, 1024).map(|_| ()).map_err(|e| e.0),
            Err("worker_artifact_root_invalid")
        );
    }
    assert_eq!(
        Disk::open(&root, 0).map(|_| ()).map_err(|e| e.0),
        Err("worker_limits_invalid")
    );
    std::fs::remove_dir_all(&root).unwrap();
}

// ---- Lane MG2 item 3 (REVIEW-laneMGF F7): the guard's speed is pinned.
// The guard waits through an injected `Pace` that hands each requested period
// to the test and advances a virtual clock by it; no wall-clock sleep orders
// anything.

/// Hands every requested period to the test, then advances the virtual clock
/// by it; `now` advances it by `step` per call (a slow filesystem). Once the
/// test is gone it waits for real, so teardown can stop the guard.
struct Ticked {
    periods: SyncSender<Duration>,
    clock: Arc<std::sync::Mutex<Duration>>,
    step: Duration,
}
impl Pace for Ticked {
    fn period(&mut self, period: Duration, rest: &mut dyn FnMut(Duration) -> bool) -> bool {
        if self.periods.send(period).is_err() {
            return rest(period);
        }
        *self.clock.lock().unwrap() += period;
        rest(Duration::ZERO)
    }
    fn now(&mut self) -> Duration {
        let mut clock = self.clock.lock().unwrap();
        *clock += self.step;
        *clock
    }
}
fn ticked(step: Duration) -> (Ticked, Receiver<Duration>, Arc<std::sync::Mutex<Duration>>) {
    let (periods, requested) = sync_channel(0);
    let clock = Arc::new(std::sync::Mutex::new(Duration::ZERO));
    let pace = Ticked {
        periods,
        clock: clock.clone(),
        step,
    };
    (pace, requested, clock)
}
fn period_row() -> Duration {
    duration(BudgetId::PyMyceliumA13MacosWorkerManagedworkerWatchSelfClosedWaitL1460).unwrap()
}

/// Every wait the guard takes is exactly one sampling period from the budgets
/// row (v0.1.4 `_closed.wait(0.25)`, worker.py:1460), and a breach that
/// appears between two samples is acted on at the next one: the verdict comes
/// with no further period asked for, at virtual time 4 periods for a breach
/// first readable at sample 4. A guard sampling 40 times slower fails here.
#[test]
fn a_breach_is_acted_on_within_one_sampling_period() {
    let row = period_row();
    let bound = duration(BudgetId::ModelSelection).unwrap();
    let (child, client) = idle_child();
    let pid = child.id();
    // Buffered by one: a guard that waits twice per sample cannot deadlock the
    // test; the virtual clock below catches it instead.
    let (readings, script) = sync_channel(1);
    let (pace, periods, clock) = ticked(Duration::ZERO);
    let worker = Worker::from_child_paced(
        child,
        client,
        flag(),
        LIMIT,
        None,
        Box::new(Scripted(script)),
        Box::new(pace),
    )
    .unwrap();
    for _ in 0..3 {
        assert_eq!(
            periods.recv_timeout(bound),
            Ok(row),
            "each wait is one sampling period: the budgets row"
        );
        readings.send(Ok(LIMIT)).unwrap();
    }
    assert_eq!(periods.recv_timeout(bound), Ok(row));
    readings.send(Ok(LIMIT + 1)).unwrap();
    let failure = worker.guard_wait_until(Instant::now() + bound);
    assert_eq!(
        failure.map(|f| (f.code, f.samples)),
        Some((MEMORY_LIMIT_EXCEEDED, 4))
    );
    assert_eq!(
        periods.recv_timeout(bound),
        Err(std::sync::mpsc::RecvTimeoutError::Disconnected),
        "the guard acted without waiting another period"
    );
    assert_eq!(
        *clock.lock().unwrap(),
        row * 4,
        "virtual time at the verdict"
    );
    assert_gone(pid);
    assert_eq!(worker.kill_and_reap().unwrap()["worker_reaped"], true);
}

/// A storage walk that cannot finish within one sampling period fails closed
/// (`worker_storage_observation_failed`); the same folder walked on a clock
/// that does not move is within the cap and leaves the worker running.
#[test]
fn a_storage_walk_that_overruns_one_period_fails_closed() {
    let row = period_row();
    let bound = duration(BudgetId::ModelSelection).unwrap();
    let root = temp_root("slow");
    for i in 0..8 {
        write(&root.join(i.to_string()), 1);
    }
    for (step, expected) in [
        (Duration::ZERO, None),
        (row / 4, Some(STORAGE_OBSERVATION_FAILED)),
    ] {
        let (child, client) = idle_child();
        let pid = child.id();
        let (readings, script) = sync_channel(1);
        let (pace, periods, _clock) = ticked(step);
        let worker = Worker::from_child_paced(
            child,
            client,
            flag(),
            LIMIT,
            Some(Disk::open(&root, 1024).unwrap()),
            Box::new(Scripted(script)),
            Box::new(pace),
        )
        .unwrap();
        readings.send(Ok(0)).unwrap();
        assert_eq!(periods.recv_timeout(bound), Ok(row));
        // The second request proves the first sample was judged; after a
        // verdict the guard asks for none.
        let next = periods.recv_timeout(bound);
        let failure = worker
            .guard_wait_until(Instant::now() + if next.is_ok() { Duration::ZERO } else { bound });
        assert_eq!(failure.map(|f| f.code), expected, "clock step {step:?}");
        if expected.is_some() {
            assert_eq!(next, Err(std::sync::mpsc::RecvTimeoutError::Disconnected));
            assert_gone(pid);
        } else {
            assert_eq!(next, Ok(row));
        }
        drop(periods);
        drop(readings);
        assert_eq!(worker.kill_and_reap().unwrap()["worker_reaped"], true);
    }
    std::fs::remove_dir_all(&root).unwrap();
}
