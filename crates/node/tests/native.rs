// SPDX-License-Identifier: AGPL-3.0-only
mod support;
use budgets::{BudgetId as B, Entries};
use node::{process::NativeChild, *};
use std::{
    path::PathBuf,
    process::Command,
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l6-native-{}-{}",
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
#[test]
fn owned_child_fixture() {
    if std::env::var_os("L6_NATIVE_FIXTURE").is_some() {
        support::require_anchor();
        support::hold();
    }
}
fn spawn(root: &std::path::Path) -> support::Reaped<NativeChild> {
    let mut cmd = Command::new(std::env::current_exe().unwrap());
    cmd.args(["--exact", "owned_child_fixture", "--nocapture"])
        .env("L6_NATIVE_FIXTURE", "hold");
    support::anchored(&mut cmd);
    let child = NativeChild::spawn(&mut cmd, root).unwrap();
    let pid = child.pid();
    support::Reaped::new(child, vec![pid])
}
fn stopped(w: &mut impl WorkerAdapter) -> std::result::Result<Residue, WorkerError> {
    w.request_stop()?;
    let end = Instant::now()
        + Duration::from_millis(B::WorkerStartup.span(Entries::ZERO).unwrap().millis());
    loop {
        let state = w.observe()?;
        if state.process_gone {
            return Ok(state);
        }
        if Instant::now() >= end {
            return Err(WorkerError::Refused("fixture_cleanup_timeout"));
        }
        std::thread::sleep(Duration::from_millis(
            B::OwnerPoll.span(Entries::ZERO).unwrap().millis(),
        ));
    }
}
#[test]
fn native_child_exit_does_not_prove_foreign_residue_gone() {
    let t = Temp::new();
    let root = t.0.join("worker");
    let mut w = spawn(&root);
    let alive = w.observe().unwrap();
    std::fs::write(root.join("foreign-sentinel"), b"keep").unwrap();
    let result = stopped(&mut w);
    assert!(!alive.process_gone);
    assert!(!alive.resources_gone);
    assert!(result.is_err(), "foreign residue must prevent proof");
    assert_eq!(
        std::fs::read(root.join("foreign-sentinel")).unwrap(),
        b"keep"
    );
    std::fs::remove_file(root.join("foreign-sentinel")).unwrap();
    let done = w.observe().unwrap();
    assert!(done.process_gone && done.resources_gone);
    assert!(!root.exists());
}
#[test]
fn native_spawn_failure_removes_only_its_new_resource_directory() {
    let t = Temp::new();
    let root = t.0.join("worker");
    let mut command = Command::new(t.0.join("not-an-executable"));
    assert!(NativeChild::spawn(&mut command, &root).is_err());
    assert!(!root.exists());
}
#[test]
fn native_unexpected_exit_cleanup_unblocks_core_without_poisoning_successor() {
    use budgets::{ClockDomain, MonoInstant, WallExpiry};
    struct Stub;
    impl Planner for Stub {
        fn plan(&mut self, m: &[PlanningMember]) -> Result<Plan> {
            Ok(Plan {
                assignments: m.iter().map(|m| (m.node.clone(), m.caps.clone())).collect(),
                stop_ms: B::ObservedStop.span(Entries::ZERO)?.millis(),
                start_ms: B::WorkerStartup.span(Entries::ZERO)?.millis(),
                safety_ms: B::WorkerCleanupReserve.span(Entries::ZERO)?.millis(),
            })
        }
    }
    let t = Temp::new();
    let now = Now {
        mono: MonoInstant::new(ClockDomain(1), 0),
        wall: WallExpiry::from_unix_millis(0),
    };
    let policy = Caps {
        storage_bytes: B::FreshStorageConsent.bytes().unwrap().get(),
        memory_bytes: B::SavedStorageConsent.bytes().unwrap().get(),
        wall_ms: B::WorkerDefaultWall.span(Entries::ZERO).unwrap().millis(),
        commands: B::ReplayKeep.count().unwrap().get(),
    };
    let (mut core, _) = Core::open(
        &t.0.join("node.db"),
        Binding {
            swarm_id: "fixture".into(),
            node_id: "owner".into(),
            seed_key_digest: "fixture-key".into(),
        },
        policy,
        now,
    )
    .unwrap();
    core.plan(&mut Stub, now).unwrap();
    let Effect::Start { ticket, .. } = core.start(now).unwrap().remove(0) else {
        panic!()
    };
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(["--exact", "owned_child_fixture"])
        .env_remove("L6_NATIVE_FIXTURE");
    let child = NativeChild::spawn(&mut command, &t.0.join("exited-worker")).unwrap();
    let mut supervisor = Supervisor::new(ticket.clone(), child);
    core.started(&ticket, now.mono, now).unwrap();
    let until = Instant::now()
        + Duration::from_millis(B::WorkerStartup.span(Entries::ZERO).unwrap().millis());
    let proof = loop {
        let p = supervisor.observe_cleanup().unwrap();
        if p.proven() {
            break p;
        }
        assert!(Instant::now() < until);
        std::thread::yield_now();
    };
    core.worker_failed(&ticket, now).unwrap();
    assert_eq!(core.state().workers.len(), 1);
    core.cleaned(&proof, now).unwrap();
    core.plan(&mut Stub, now).unwrap();
    let Effect::Start { ticket: next, .. } = core.start(now).unwrap().remove(0) else {
        panic!()
    };
    assert_ne!(ticket, next);
    assert!(core.cleaned(&proof, now).is_err());
    assert_eq!(core.state().workers.len(), 1);
}
#[test]
fn native_healthy_cleanup_is_idempotent_and_exactly_owned() {
    let t = Temp::new();
    let root = t.0.join("worker");
    let sibling = t.0.join("sibling");
    std::fs::create_dir(&sibling).unwrap();
    let mut w = spawn(&root);
    let result = stopped(&mut w).unwrap();
    assert!(result.process_gone && result.resources_gone);
    assert!(!root.exists());
    assert!(sibling.is_dir());
    assert_eq!(w.observe().unwrap(), result);
}
