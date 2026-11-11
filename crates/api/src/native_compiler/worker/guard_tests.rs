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
    process::{Child, Command, Stdio},
    sync::{
        Arc,
        atomic::AtomicBool,
        mpsc::{Receiver, sync_channel},
    },
    time::Instant,
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
        Box::new(Scripted(script)),
    );
    assert!(feeder.join().unwrap(), "the guard must take both readings");
    assert_eq!(
        started.map(|_| ()).map_err(|e| e.0),
        Err(MEMORY_LIMIT_EXCEEDED)
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
/// host probe sees it and the guard kills and reaps it within two samples of
/// the memory being resident. No model, no Metal.
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
        worker.guard_wait_until(Instant::now() + duration(BudgetId::ModelSelection).unwrap());
    assert_eq!(
        failure.as_ref().map(|f| f.code),
        Some(MEMORY_LIMIT_EXCEEDED),
        "resident marker seen: {resident}"
    );
    let failure = failure.unwrap();
    assert!(
        resident,
        "the guard stopped the child before it was resident"
    );
    assert!(
        failure.samples <= base + 2,
        "{} samples after the memory was resident at sample {base}",
        failure.samples
    );
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

/// Lane MG item 4 RED (ignored: not built; design in REPORT-laneMG.md). v0.1.4
/// walks the worker's artifact root every 0.25 s and stops the worker with
/// `worker_disk_limit_exceeded` once its regular files exceed `max_disk_bytes`
/// (worker.py:820-844). v0.2 checks the shard size only once, at admission.
#[test]
#[ignore = "lane MG item 4 RED: the disk guard is not built yet (see REPORT-laneMG.md)"]
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
