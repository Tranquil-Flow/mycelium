// SPDX-License-Identifier: AGPL-3.0-only
//! V03 characterization (integrator ruling 3): v0.1.4 serialises cache operations with a blocking
//! `fcntl.flock(fd, LOCK_EX)` and no timeout (`mycelium_assignment_cache.py:_locked` @ f4e366e9),
//! so a second holder BLOCKS until the first operation ends; it does not fail with a "busy"
//! error. v0.2 keeps that behaviour. A bounded wait that refuses after a budget is a deliberate
//! V03 change and must replace this test, not be slipped in beside it.
//!
//! Two processes: this test binary re-executes itself as the lock holder, so the contender and
//! the holder have separate open file descriptions in separate processes (as the app and a
//! script would). The Python oracle's own two-process run is recorded in
//! `L7-evidence/round8-lock-contention-oracle.log`.
#[path = "support/temp.rs"]
mod test_temp;
use artifacts::Cache;
use budgets::{BudgetId as B, Bytes, Entries};
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::mpsc,
    time::Duration,
};

const ROOT: &str = "MYCELIUM_L7_LOCK_ROOT";
const PARENT: &str = "MYCELIUM_L7_LOCK_PARENT";
const MIDDLE: &str = "MYCELIUM_L7_LOCK_MIDDLE";

fn duration(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}

fn open(root: &Path) -> Cache {
    Cache::open(root, Bytes::new(1024), Bytes::new(65536), Bytes::new(4096)).unwrap()
}

/// The lock-holder child (a single process). Every exit path of the owning test (assert, panic,
/// timeout, a mutant that makes the contender hang) releases it: `Drop` writes the release file,
/// kills it and reaps it, so no holder can outlive its test.
struct Holder {
    child: Child,
    release: PathBuf,
}

impl Holder {
    fn spawn(ws: &Path) -> Self {
        let child = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "lock_holder_child", "--nocapture"])
            .env(ROOT, ws)
            .env(PARENT, std::process::id().to_string())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        Self {
            child,
            release: ws.join("release"),
        }
    }
}

impl Drop for Holder {
    fn drop(&mut self) {
        // Cleanup must not panic inside a panicking test: failures are recorded, not raised.
        if let Err(e) = fs::write(&self.release, b"go") {
            eprintln!("holder release file: {e}");
        }
        // `Child::kill` is a no-op once the child was reaped, so a reused pid is never signalled.
        if let Err(e) = self.child.kill() {
            eprintln!("holder kill: {e}");
        }
        if let Err(e) = self.child.wait() {
            eprintln!("holder wait: {e}");
        }
    }
}

fn alive(pid: u32) -> bool {
    Command::new("/bin/kill")
        .args(["-0", &pid.to_string()])
        .stderr(Stdio::null())
        .status()
        .unwrap()
        .success()
}

/// Not a test of its own: the lock-holder process. It does nothing unless the parent test set
/// the root variable. It also leaves when its parent is gone (SIGKILLed test runner).
#[test]
fn lock_holder_child() {
    let Ok(root) = std::env::var(ROOT) else {
        return;
    };
    let parent: u32 = std::env::var(PARENT).unwrap().parse().unwrap();
    let root = Path::new(&root);
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(root.join("cache/.cache.lock"))
        .unwrap();
    lock.lock().unwrap();
    fs::write(root.join("ready"), b"locked").unwrap();
    while !root.join("release").exists() && std::os::unix::process::parent_id() == parent {
        std::thread::sleep(duration(B::RegistryPoll));
    }
    lock.unlock().unwrap();
}

/// Not a test of its own: spawns a holder and exits at once without reaping it.
#[test]
fn lock_holder_orphaner() {
    let Ok(root) = std::env::var(MIDDLE) else {
        return;
    };
    let ws = Path::new(&root);
    let holder = Holder::spawn(ws);
    fs::write(ws.join("holder-pid"), holder.child.id().to_string()).unwrap();
    std::mem::forget(holder);
    std::process::exit(0);
}

#[test]
fn a_holder_never_outlives_its_test_on_panic_or_when_its_parent_is_killed() {
    let ws = test_temp::directory("lock-holder-reaping");
    let _cache = open(&ws.join("cache"));
    // 1. The owning test panics while the holder runs: Drop reaps it.
    let pid = std::panic::catch_unwind(|| {
        let holder = Holder::spawn(&ws);
        let pid = holder.child.id();
        assert!(alive(pid));
        std::panic::resume_unwind(Box::new(pid));
    })
    .unwrap_err()
    .downcast::<u32>()
    .unwrap();
    assert!(!alive(*pid), "holder survived a panicking test");
    // 2. The parent dies without reaping (SIGKILL equivalent: it exits and forgets the guard):
    // the orphaned holder (ppid 1) leaves on its own.
    let ws2 = test_temp::directory("lock-holder-orphan");
    drop(open(&ws2.join("cache")));
    let status = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "lock_holder_orphaner", "--nocapture"])
        .env(MIDDLE, &ws2)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .unwrap();
    assert!(status.success());
    let orphan: u32 = fs::read_to_string(ws2.join("holder-pid"))
        .unwrap()
        .parse()
        .unwrap();
    let deadline = std::time::Instant::now() + duration(B::WorkerStartup);
    while alive(orphan) && std::time::Instant::now() < deadline {
        std::thread::sleep(duration(B::RegistryPoll));
    }
    let survived = alive(orphan);
    if survived {
        // Last resort: the orphan this test created (exact pid).
        let killed = Command::new("/bin/kill")
            .args(["-KILL", &orphan.to_string()])
            .status();
        eprintln!("orphaned holder {orphan} killed by the test: {killed:?}");
    }
    assert!(!survived, "an orphaned holder kept running");
    fs::remove_dir_all(ws).unwrap();
    fs::remove_dir_all(ws2).unwrap();
}

#[test]
fn v03_characterization_a_second_lock_holder_blocks_until_release_as_v014_does() {
    let ws = test_temp::directory("lock-contention");
    let cache = open(&ws.join("cache"));
    let mut holder = Holder::spawn(&ws);
    while !ws.join("ready").exists() {
        assert!(
            holder.child.try_wait().unwrap().is_none(),
            "holder exited early"
        );
        std::thread::sleep(duration(B::RegistryPoll));
    }
    let (tx, rx) = mpsc::channel();
    let contender = std::thread::spawn(move || {
        let outcome = cache.status();
        tx.send(outcome.is_ok()).unwrap();
        cache
    });
    // Held by the other process: the operation neither completes nor fails with a busy error.
    assert_eq!(
        rx.recv_timeout(duration(B::RegistryPoll)),
        Err(mpsc::RecvTimeoutError::Timeout)
    );
    fs::write(ws.join("release"), b"go").unwrap();
    assert_eq!(rx.recv_timeout(duration(B::WorkerStartup)), Ok(true));
    let cache = contender.join().unwrap();
    assert!(holder.child.wait().unwrap().success());
    drop(cache);
    fs::remove_dir_all(ws).unwrap();
}
