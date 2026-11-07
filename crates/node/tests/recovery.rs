// SPDX-License-Identifier: AGPL-3.0-only
// O:path:mycelium_node/process.py; F3/F16. Real inert child/grandchild, no model or app.
mod support;
use budgets::{BudgetId as B, Entries};
use control_runtime::Store;
use node::{
    recovery::{GroupChild, recover},
    *,
};
use std::{
    path::PathBuf,
    process::{Child, Command},
    time::{Duration, Instant},
};
fn ticket() -> Ticket {
    Ticket {
        epoch: 1,
        run: 1,
        node: "fixture".into(),
        incarnation: "owner-1".into(),
    }
}
fn until() -> Instant {
    Instant::now() + Duration::from_millis(B::WorkerStartup.span(Entries::ZERO).unwrap().millis())
}
fn wait(mut condition: impl FnMut() -> bool) {
    let end = until();
    while !condition() {
        assert!(Instant::now() < end, "fixture deadline");
        std::thread::sleep(Duration::from_millis(
            B::OwnerPoll.span(Entries::ZERO).unwrap().millis(),
        ));
    }
}
// EPERM from an all-zombie Darwin group is uncertainty, never cleanup proof.
// Keep the same bounded deadline and require the final positive observation.
fn cleanup_proven(observation: std::result::Result<CleanupObservation, WorkerError>) -> bool {
    match observation {
        Ok(proof) => proof.proven(),
        Err(WorkerError::Io(error)) if error.kind() == std::io::ErrorKind::PermissionDenied => {
            eprintln!("fixture retains reservation during group probe denial: {error}");
            false
        }
        Err(error) => panic!("unexpected cleanup failure: {error:?}"),
    }
}
fn command(role: &str, p: &std::path::Path) -> Command {
    let mut c = Command::new(std::env::current_exe().unwrap());
    c.args(["--exact", "family_fixture", "--nocapture"])
        .env("L6_RECOVERY_ROLE", role)
        .env("L6_RECOVERY_ROOT", p);
    support::anchored(&mut c);
    c
}
#[test]
fn live_group_cleanup_proves_descendants_and_refuses_foreign_resource_replacement() {
    let t = Temp::new();
    let mut store = Store::open(&t.0.join("state.db")).unwrap();
    let resource = t.0.join("owned");
    let worker = GroupChild::spawn(
        &mut command("leader", &t.0),
        &resource,
        &mut store,
        "live",
        ticket(),
    )
    .unwrap();
    let mut supervisor = Supervisor::new(ticket(), worker);
    wait(|| t.0.join("descendant-ready").exists());
    assert!(!cleanup_proven(supervisor.observe_cleanup()));
    supervisor.request_stop().unwrap();
    wait(|| cleanup_proven(supervisor.observe_cleanup()));
    assert!(!resource.exists());
    let mut c = Command::new("/usr/bin/true");
    let worker = GroupChild::spawn(&mut c, &resource, &mut store, "replacement", ticket()).unwrap();
    std::fs::rename(&resource, t.0.join("original-resource")).unwrap();
    std::fs::create_dir(&resource).unwrap();
    let mut supervisor = Supervisor::new(ticket(), worker);
    wait(|| match supervisor.observe_cleanup() {
        Err(WorkerError::Refused("worker_resource_identity_changed")) => true,
        Err(WorkerError::Io(error)) if error.kind() == std::io::ErrorKind::PermissionDenied => {
            eprintln!("fixture retains reservation during transient group probe denial: {error}");
            false
        }
        Err(error) => panic!("replacement must reach the identity guard: {error:?}"),
        Ok(proof) => {
            assert!(!proof.proven(), "replaced empty directory is not ours");
            false
        }
    });
    std::fs::write(resource.join("foreign"), b"keep").unwrap();
    assert_eq!(std::fs::read(resource.join("foreign")).unwrap(), b"keep");
    drop(supervisor);
    drop(store);
    let path = t.0.clone();
    drop(t);
    std::fs::remove_dir_all(path).unwrap();
}
#[test]
fn exited_leaf_group_is_reaped_before_independent_absence_proof() {
    let t = Temp::new();
    let mut store = Store::open(&t.0.join("state.db")).unwrap();
    let mut command = Command::new("/usr/bin/true");
    let worker = GroupChild::spawn(
        &mut command,
        &t.0.join("owned"),
        &mut store,
        "leaf",
        ticket(),
    )
    .unwrap();
    let mut supervisor = Supervisor::new(ticket(), worker);
    wait(|| cleanup_proven(supervisor.observe_cleanup()));
    drop(supervisor);
    drop(store);
    let path = t.0.clone();
    drop(t);
    std::fs::remove_dir_all(path).unwrap();
}
#[test]
fn exited_leader_keeps_exact_group_authority_until_descendants_are_stopped() {
    use rustix::process::{WaitId, WaitIdOptions, waitid};
    let t = Temp::new();
    let mut store = Store::open(&t.0.join("state.db")).unwrap();
    let worker = GroupChild::spawn(
        &mut command("leader", &t.0),
        &t.0.join("owned"),
        &mut store,
        "live",
        ticket(),
    )
    .unwrap();
    let record: serde_json::Value =
        serde_json::from_slice(&store.load_document("live").unwrap().unwrap().1).unwrap();
    let pid = rustix::process::Pid::from_raw(record["group"].as_i64().unwrap() as i32).unwrap();
    let mut supervisor = Supervisor::new(ticket(), worker);
    wait(|| t.0.join("descendant-ready").exists());
    std::fs::write(t.0.join("leader-exit"), b"exit").unwrap();
    wait(|| {
        waitid(
            WaitId::Pid(pid),
            WaitIdOptions::EXITED | WaitIdOptions::NOHANG | WaitIdOptions::NOWAIT,
        )
        .unwrap()
        .is_some()
    });
    // Observation must not discard the unreaped Child identity before cleanup.
    let _initial_proof = cleanup_proven(supervisor.observe_cleanup());
    let stopped = supervisor.request_stop();
    if stopped.is_err() {
        // Independent fixture release keeps the RED contained and bounded.
        std::fs::write(t.0.join("descendant-exit"), b"exit").unwrap();
        wait(|| cleanup_proven(supervisor.observe_cleanup()));
    }
    assert!(
        stopped.is_ok(),
        "reaping the leader discarded safe descendant termination authority: {stopped:?}"
    );
    wait(|| cleanup_proven(supervisor.observe_cleanup()));
    drop(supervisor);
    drop(store);
    let root = t.0.clone();
    drop(t);
    std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn external_reaping_revokes_numeric_group_signal_authority() {
    use rustix::process::{WaitId, WaitIdOptions, WaitOptions, waitid, waitpid};
    let t = Temp::new();
    let mut store = Store::open(&t.0.join("state.db")).unwrap();
    let mut worker = GroupChild::spawn(
        &mut command("leader", &t.0),
        &t.0.join("owned"),
        &mut store,
        "live",
        ticket(),
    )
    .unwrap();
    let record: serde_json::Value =
        serde_json::from_slice(&store.load_document("live").unwrap().unwrap().1).unwrap();
    let pid = rustix::process::Pid::from_raw(record["group"].as_i64().unwrap() as i32).unwrap();
    wait(|| t.0.join("descendant-ready").exists());
    std::fs::write(t.0.join("leader-exit"), b"exit").unwrap();
    wait(|| {
        waitid(
            WaitId::Pid(pid),
            WaitIdOptions::EXITED | WaitIdOptions::NOHANG | WaitIdOptions::NOWAIT,
        )
        .unwrap()
        .is_some()
    });
    waitpid(Some(pid), WaitOptions::empty()).unwrap();
    let stopped = worker.request_stop();
    std::fs::write(t.0.join("descendant-exit"), b"exit").unwrap();
    let mut recovery = recover(&store, "live", &ticket()).unwrap();
    wait(|| match recovery.observe_cleanup() {
        Ok(observation) => observation.proven(),
        Err(WorkerError::Io(error)) if error.kind() == std::io::ErrorKind::PermissionDenied => {
            eprintln!(
                "fixture recovery retains reservation during transient group probe denial: {error}"
            );
            false
        }
        Err(error) => panic!("fixture recovery failed: {error:?}"),
    });
    assert!(
        matches!(
            stopped,
            Err(WorkerError::Refused("worker_exact_child_ownership_lost"))
        ),
        "lost parent-child identity must revoke signalling, got {stopped:?}"
    );
    drop(worker);
    drop(recovery);
    drop(store);
    let root = t.0.clone();
    drop(t);
    std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn prepared_spawn_never_invents_process_absence() {
    let t = Temp::new();
    let mut store = Store::open(&t.0.join("state.db")).unwrap();
    let record = serde_json::json!({"ticket":ticket(),"root":t.0.join("unknown"),"device":0,"inode":0,"group":null});
    store
        .save_document("prepared", None, &serde_json::to_vec(&record).unwrap())
        .unwrap();
    assert!(recover(&store, "prepared", &ticket()).is_err());
    drop(store);
    let path = t.0.clone();
    drop(t);
    std::fs::remove_dir_all(path).unwrap();
}

#[test]
fn family_fixture() {
    let Some(role) = std::env::var_os("L6_RECOVERY_ROLE") else {
        return;
    };
    let p = PathBuf::from(std::env::var_os("L6_RECOVERY_ROOT").unwrap());
    match role.to_str().unwrap() {
        "owner" => {
            let mut store = Store::open(&p.join("journal.db")).unwrap();
            let _owned = GroupChild::spawn(
                &mut command("leader", &p),
                &p.join("resources"),
                &mut store,
                "worker",
                ticket(),
            )
            .unwrap();
            std::fs::write(p.join("owner-ready"), b"ready").unwrap();
            wait(|| p.join("owner-exit").exists());
        }
        "leader" => {
            let mut child = Owner(command("descendant", &p).spawn().unwrap());
            std::fs::write(p.join("leader-ready"), b"ready").unwrap();
            let end = until();
            while !p.join("leader-exit").exists() && Instant::now() < end {
                std::thread::sleep(Duration::from_millis(
                    B::OwnerPoll.span(Entries::ZERO).unwrap().millis(),
                ));
            }
            if Instant::now() >= end {
                child.0.kill().unwrap();
                child.0.wait().unwrap();
            } else {
                // Deliberately orphan our inert descendant. No product code
                // or model is launched; the descendant has its own deadline.
                std::process::exit(0);
            }
        }
        "descendant" => {
            std::fs::write(p.join("descendant-ready"), b"ready").unwrap();
            let end = until();
            while !p.join("descendant-exit").exists() && Instant::now() < end {
                std::thread::sleep(Duration::from_millis(
                    B::OwnerPoll.span(Entries::ZERO).unwrap().millis(),
                ));
            }
        }
        _ => panic!("unknown role"),
    }
}
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l6-recovery-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir(&p).unwrap();
        Self(p)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        std::fs::write(self.0.join("leader-exit"), b"exit").unwrap();
        std::fs::write(self.0.join("descendant-exit"), b"exit").unwrap();
    }
}
struct Owner(Child);
impl Drop for Owner {
    fn drop(&mut self) {
        if self.0.try_wait().unwrap().is_none() {
            self.0.kill().unwrap();
            self.0.wait().unwrap();
        }
    }
}
#[test]
fn supervisor_crash_recovery_waits_for_descendants_and_refuses_foreign_resources() {
    struct PlannerFixture;
    impl Planner for PlannerFixture {
        fn plan(&mut self, m: &[PlanningMember]) -> node::Result<Plan> {
            Ok(Plan {
                assignments: m.iter().map(|m| (m.node.clone(), m.caps.clone())).collect(),
                stop_ms: B::ObservedStop.span(Entries::ZERO)?.millis(),
                start_ms: B::WorkerStartup.span(Entries::ZERO)?.millis(),
                safety_ms: B::WorkerCleanupReserve.span(Entries::ZERO)?.millis(),
            })
        }
    }
    let t = Temp::new();
    let binding = Binding {
        swarm_id: "swarm".into(),
        node_id: "fixture".into(),
        seed_key_digest: "fixture-key".into(),
    };
    let policy = Caps {
        storage_bytes: B::FreshStorageConsent.bytes().unwrap().get(),
        memory_bytes: B::SavedStorageConsent.bytes().unwrap().get(),
        wall_ms: B::WorkerDefaultWall.span(Entries::ZERO).unwrap().millis(),
        commands: B::ReplayKeep.count().unwrap().get(),
    };
    let now = Now {
        mono: budgets::MonoInstant::new(budgets::ClockDomain(1), 0),
        wall: budgets::WallExpiry::from_unix_millis(0),
    };
    let (mut core, _) =
        Core::open(&t.0.join("core.db"), binding.clone(), policy.clone(), now).unwrap();
    core.plan(&mut PlannerFixture, now).unwrap();
    assert!(matches!(&core.start(now).unwrap()[0],Effect::Start{ticket:t,..} if t==&ticket()));
    drop(core);
    let mut owner = Owner(command("owner", &t.0).spawn().unwrap());
    wait(|| t.0.join("owner-ready").exists() && t.0.join("descendant-ready").exists());
    owner.0.kill().unwrap();
    owner.0.wait().unwrap();
    let store = Store::open(&t.0.join("journal.db")).unwrap();
    let (mut core, _) = Core::open(&t.0.join("core.db"), binding, policy, now).unwrap();
    let mut recovered = recover(&store, "worker", &ticket()).unwrap();
    assert!(
        core.cleaned(&recovered.observe_cleanup().unwrap(), now)
            .is_err()
    );
    assert_eq!(core.state().workers.len(), 1);
    assert!(!recovered.observe_cleanup().unwrap().proven());
    assert!(
        recovered.request_stop().is_err(),
        "lost Child handle must not signal a potentially reused group"
    );
    std::fs::write(t.0.join("leader-exit"), b"exit").unwrap();
    let record: serde_json::Value =
        serde_json::from_slice(&store.load_document("worker").unwrap().unwrap().1).unwrap();
    let leader =
        rustix::process::Pid::from_raw(i32::try_from(record["group"].as_u64().unwrap()).unwrap())
            .unwrap();
    wait(|| {
        matches!(
            rustix::process::test_kill_process(leader),
            Err(rustix::io::Errno::SRCH)
        )
    });
    assert!(
        !recovered.observe_cleanup().unwrap().proven(),
        "descendant remains"
    );
    std::fs::write(t.0.join("resources/foreign"), b"keep").unwrap();
    std::fs::write(t.0.join("descendant-exit"), b"exit").unwrap();
    wait(|| recovered.observe_cleanup().is_err());
    assert_eq!(
        std::fs::read(t.0.join("resources/foreign")).unwrap(),
        b"keep"
    );
    std::fs::remove_file(t.0.join("resources/foreign")).unwrap();
    assert!(recovered.observe_cleanup().unwrap().proven());
    assert!(recovered.observe_cleanup().unwrap().proven());
    let proof = recovered.observe_cleanup().unwrap();
    core.cleaned(&proof, now).unwrap();
    assert!(core.state().workers.is_empty());
    core.plan(&mut PlannerFixture, now).unwrap();
    core.start(now).unwrap();
    assert!(core.cleaned(&proof, now).is_err());
    assert_eq!(core.state().workers.len(), 1);
    drop(core);
    let mut other = ticket();
    other.run = 2;
    assert!(recover(&store, "worker", &other).is_err());
    let path = t.0.clone();
    drop(recovered);
    drop(store);
    drop(owner);
    drop(t);
    std::fs::remove_dir_all(path).unwrap();
}
