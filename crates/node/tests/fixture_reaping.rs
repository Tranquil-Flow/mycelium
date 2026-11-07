// SPDX-License-Identifier: AGPL-3.0-only
// BACKLOG 17: no fixture child may outlive its test, whichever way the test ends
// (pass, assertion failure, panic), and no fixture may outlive its owner (parent
// SIGKILLed). A leaked fixture held loopback sockets for hours in earlier rounds.
// Every scenario records the exact pids it spawned and a janitor SIGKILLs any that
// survive, so even a RED run of this file cannot leave an orphan behind.
mod support;
use node::{
    guardian::{PersistentChild, WorkerCommand},
    process::NativeChild,
    recovery::GroupChild,
    *,
};
use std::{
    cell::RefCell,
    panic::{AssertUnwindSafe, catch_unwind},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    time::Instant,
};
use support::{Reaped, anchor_value, anchored, anchored_to_this_process, bounded, gone, poll};

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l6-reap-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::create_dir(&p).unwrap();
        Self(p)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        // Best effort: the assertion that matters already ran or is unwinding.
        if let Err(error) = std::fs::remove_dir_all(&self.0) {
            eprintln!("temp cleanup: {error}");
        }
    }
}
/// Exact-pid last resort for THIS file's own meta-tests only.
struct Janitor(RefCell<Vec<u32>>);
impl Janitor {
    fn note(&self, pid: u32) {
        self.0.borrow_mut().push(pid);
    }
}
impl Drop for Janitor {
    fn drop(&mut self) {
        for pid in self.0.borrow().iter().copied() {
            if !gone(pid) {
                eprintln!("janitor: fixture {pid} survived its test");
                if let Err(error) = rustix::process::kill_process(
                    rustix::process::Pid::from_raw(pid as i32).unwrap(),
                    rustix::process::Signal::KILL,
                ) {
                    eprintln!("janitor: kill {pid}: {error}");
                }
            }
        }
    }
}
fn wait(what: &str, mut condition: impl FnMut() -> bool) {
    let end = Instant::now() + bounded();
    while !condition() {
        assert!(Instant::now() < end, "deadline: {what}");
        std::thread::sleep(poll());
    }
}
fn fixture_arguments() -> Vec<std::ffi::OsString> {
    ["--exact", "reaping_fixture", "--nocapture"]
        .map(Into::into)
        .into()
}
/// Worker side (also the middleman side). No-op unless launched as a fixture.
#[test]
fn reaping_fixture() {
    let Some(role) = std::env::var_os("L6_REAP_ROLE") else {
        return;
    };
    support::require_anchor();
    if let Some(ready) = std::env::var_os("L6_REAP_READY") {
        std::fs::write(ready, b"ready").unwrap();
    }
    if role == "middleman" {
        let pidfile = PathBuf::from(std::env::var_os("L6_REAP_PIDFILE").unwrap());
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args(fixture_arguments())
            .env("L6_REAP_ROLE", "hold")
            .env(
                "L6_REAP_READY",
                std::env::var_os("L6_REAP_CHILD_READY").unwrap(),
            )
            .env_remove("L6_REAP_PIDFILE")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        support::anchored_to_this_process(&mut command);
        let child = command.spawn().unwrap();
        std::fs::write(&pidfile, child.id().to_string()).unwrap();
        // The std Child handle is dropped without wait: this process is the child's
        // only owner and is about to be SIGKILLed.
        std::mem::forget(child);
    }
    support::hold();
}
fn fixture_command(ready: &Path) -> Command {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(fixture_arguments())
        .env("L6_REAP_ROLE", "hold")
        .env("L6_REAP_READY", ready);
    anchored(&mut command);
    command
}
fn ticket() -> Ticket {
    Ticket {
        epoch: 1,
        run: 1,
        node: "owned".into(),
        incarnation: "reap".into(),
    }
}
#[derive(Clone, Copy, Debug)]
enum Kind {
    Native,
    Group,
    Guardian,
}
/// Spawn one fixture of `kind` under a guard, optionally panic with it still running,
/// then require every spawned pid to be gone when the guard has dropped.
fn scenario(kind: Kind, panic_inside: bool) {
    let t = Temp::new();
    let janitor = Janitor(RefCell::new(Vec::new()));
    let ready = t.0.join("ready");
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        let _guard: Box<dyn WorkerAdapter> = match kind {
            Kind::Native => {
                let child =
                    NativeChild::spawn(&mut fixture_command(&ready), &t.0.join("w")).unwrap();
                let pid = child.pid();
                janitor.note(pid);
                Box::new(Reaped::new(child, vec![pid]))
            }
            Kind::Group => {
                let mut store = control_runtime::Store::open(&t.0.join("state.db")).unwrap();
                let child = GroupChild::spawn(
                    &mut fixture_command(&ready),
                    &t.0.join("w"),
                    &mut store,
                    "reap",
                    ticket(),
                )
                .unwrap();
                let pid = child.pid();
                janitor.note(pid);
                Box::new(Reaped::new(child, vec![pid]))
            }
            Kind::Guardian => {
                let worker = WorkerCommand {
                    program: std::env::current_exe().unwrap(),
                    arguments: fixture_arguments(),
                    environment: vec![
                        ("L6_REAP_ROLE".into(), Some("hold".into())),
                        ("L6_REAP_READY".into(), Some(ready.as_os_str().into())),
                        (support::ANCHOR.into(), Some(anchor_value())),
                    ],
                };
                let child = PersistentChild::spawn(
                    Path::new(env!("CARGO_BIN_EXE_node-guardian")),
                    &worker,
                    &t.0.join("w"),
                    &t.0.join("state.db"),
                    "reap",
                    ticket(),
                )
                .unwrap();
                let (guardian, worker) = child.process_ids().unwrap();
                janitor.note(guardian);
                janitor.note(worker);
                Box::new(Reaped::new(child, vec![guardian, worker]))
            }
        };
        wait("fixture ready", || ready.exists());
        assert!(
            janitor.0.borrow().iter().all(|p| !gone(*p)),
            "{kind:?}: fixture must be running before the body ends"
        );
        if panic_inside {
            panic!("simulated failing test body");
        }
    }));
    assert_eq!(outcome.is_err(), panic_inside);
    for pid in janitor.0.borrow().iter().copied() {
        assert!(gone(pid), "{kind:?}: fixture {pid} outlived its test");
    }
}
#[test]
fn native_fixture_is_reaped_when_the_test_body_ends_normally() {
    scenario(Kind::Native, false);
}
#[test]
fn native_fixture_is_reaped_when_the_test_body_panics() {
    scenario(Kind::Native, true);
}
#[test]
fn group_fixture_is_reaped_when_the_test_body_ends_normally() {
    scenario(Kind::Group, false);
}
#[test]
fn group_fixture_is_reaped_when_the_test_body_panics() {
    scenario(Kind::Group, true);
}
#[test]
fn guardian_fixture_is_reaped_when_the_test_body_ends_normally() {
    scenario(Kind::Guardian, false);
}
#[test]
fn guardian_fixture_is_reaped_when_the_test_body_panics() {
    scenario(Kind::Guardian, true);
}
/// The owner is SIGKILLed with no destructor run: the fixture must notice and exit.
#[test]
fn fixture_exits_by_itself_when_its_owner_is_killed() {
    let t = Temp::new();
    let janitor = Janitor(RefCell::new(Vec::new()));
    let pidfile = t.0.join("child-pid");
    let mut middleman = Command::new(std::env::current_exe().unwrap());
    middleman
        .args(fixture_arguments())
        .env("L6_REAP_ROLE", "middleman")
        .env("L6_REAP_READY", t.0.join("middleman-ready"))
        .env("L6_REAP_CHILD_READY", t.0.join("child-ready"))
        .env("L6_REAP_PIDFILE", &pidfile)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    anchored_to_this_process(&mut middleman);
    let mut middleman = middleman.spawn().unwrap();
    janitor.note(middleman.id());
    wait("fixture child announced", || {
        pidfile.exists() && t.0.join("child-ready").exists()
    });
    let child: u32 = std::fs::read_to_string(&pidfile).unwrap().parse().unwrap();
    janitor.note(child);
    assert!(
        !gone(child),
        "fixture child must be running before its owner dies"
    );
    middleman.kill().unwrap();
    middleman.wait().unwrap();
    wait("orphaned fixture exits by itself", || gone(child));
}
/// An unowned fixture is a harness bug: it must refuse (exit 2) before announcing readiness,
/// so a fixture whose anchor was never exported can never silently outlive its owner.
#[test]
fn fixture_without_an_owner_anchor_refuses_to_run() {
    let t = Temp::new();
    let ready = t.0.join("ready");
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(fixture_arguments())
        .env("L6_REAP_ROLE", "hold")
        .env("L6_REAP_READY", &ready)
        .env_remove(support::ANCHOR)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    let status = support::Owned(command.spawn().unwrap()).0.wait().unwrap();
    assert_eq!(status.code(), Some(2));
    assert!(
        !ready.exists(),
        "refused fixture must not announce readiness"
    );
}
/// `anchored` must put the owner into the child's environment (the contract the
/// reaping depends on): a command built through it launches a fixture that runs.
#[test]
fn anchored_command_exports_the_owner_pid() {
    let mut command = Command::new("/usr/bin/true");
    anchored(&mut command);
    let exported = command
        .get_envs()
        .find(|(k, _)| *k == support::ANCHOR)
        .and_then(|(_, v)| v)
        .map(ToOwned::to_owned);
    assert_eq!(exported, Some(anchor_value()));
}
