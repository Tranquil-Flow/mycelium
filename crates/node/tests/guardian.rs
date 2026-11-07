// SPDX-License-Identifier: AGPL-3.0-only
// Exact-owner-death composition. No app, model, sockets or process-name scans.
mod support;
use budgets::{BudgetId as B, Entries};
use node::{
    guardian::{self, PersistentChild, WorkerCommand},
    *,
};
use std::{
    path::{Path, PathBuf},
    process::{Child, Command},
    time::{Duration, Instant},
};
fn duration(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn ticket() -> Ticket {
    Ticket {
        epoch: 1,
        run: 1,
        node: "owned".into(),
        incarnation: "owner-1".into(),
    }
}
fn command(role: &str, root: &Path) -> Command {
    let mut c = Command::new(std::env::current_exe().unwrap());
    c.args(["--exact", "persistent_fixture", "--nocapture"])
        .env("L6_GUARDIAN_ROLE", role)
        .env("L6_GUARDIAN_ROOT", root);
    support::anchored(&mut c);
    c
}
fn worker_command(root: &Path) -> WorkerCommand {
    WorkerCommand {
        program: std::env::current_exe().unwrap(),
        arguments: vec![
            "--exact".into(),
            "persistent_fixture".into(),
            "--nocapture".into(),
        ],
        environment: vec![
            ("L6_GUARDIAN_ROLE".into(), Some("worker".into())),
            ("L6_GUARDIAN_ROOT".into(), Some(root.as_os_str().into())),
            (support::ANCHOR.into(), Some(support::anchor_value())),
        ],
    }
}
fn now() -> Now {
    Now {
        mono: budgets::MonoInstant::new(budgets::ClockDomain(1), 0),
        wall: budgets::WallExpiry::from_unix_millis(0),
    }
}
fn binding() -> Binding {
    Binding {
        node_id: "owned".into(),
        swarm_id: "fixture".into(),
        seed_key_digest: "seed".into(),
    }
}
fn caps() -> Caps {
    Caps {
        storage_bytes: B::FreshStorageConsent.bytes().unwrap().get(),
        memory_bytes: B::SavedStorageConsent.bytes().unwrap().get(),
        wall_ms: duration(B::WorkerDefaultWall).as_millis() as u64,
        commands: B::ReplayKeep.count().unwrap().get(),
    }
}
struct PlannerFixture;
impl Planner for PlannerFixture {
    fn plan(&mut self, members: &[PlanningMember]) -> Result<Plan> {
        Ok(Plan {
            assignments: members
                .iter()
                .map(|m| (m.node.clone(), m.caps.clone()))
                .collect(),
            stop_ms: duration(B::ObservedStop).as_millis() as u64,
            start_ms: duration(B::WorkerStartup).as_millis() as u64,
            safety_ms: duration(B::WorkerCleanupReserve).as_millis() as u64,
        })
    }
}
fn wait(mut condition: impl FnMut() -> bool, budget: B) -> bool {
    let end = Instant::now() + duration(budget);
    loop {
        if condition() {
            return true;
        }
        if Instant::now() >= end {
            return false;
        }
        std::thread::sleep(duration(B::OwnerPoll));
    }
}
fn gone(pid: u32) -> bool {
    match rustix::process::test_kill_process(rustix::process::Pid::from_raw(pid as i32).unwrap()) {
        Err(rustix::io::Errno::SRCH) => true,
        Ok(()) | Err(rustix::io::Errno::PERM) => false,
        Err(e) => panic!("pid observation failed: {e}"),
    }
}
#[test]
fn persistent_fixture() {
    let Some(role) = std::env::var_os("L6_GUARDIAN_ROLE") else {
        return;
    };
    let root = PathBuf::from(std::env::var_os("L6_GUARDIAN_ROOT").unwrap());
    if role == "worker" || role == "descendant" {
        let mut descendant = if role == "worker" && root.join("family").exists() {
            let child = command("descendant", &root).spawn().unwrap();
            std::fs::write(root.join("descendant-pid"), child.id().to_string()).unwrap();
            Some(child)
        } else {
            None
        };
        std::fs::write(
            root.join(if role == "worker" {
                "worker-ready"
            } else {
                "descendant-ready"
            }),
            b"ready",
        )
        .unwrap();
        assert!(wait(
            || root.join("fixture-exit").exists() || support::owner_gone(),
            B::WorkerMaximumWall
        ));
        if let Some(child) = descendant.as_mut() {
            child.wait().unwrap();
        }
    } else {
        let (mut core, _) = Core::open(&root.join("node.db"), binding(), caps(), now()).unwrap();
        core.plan(&mut PlannerFixture, now()).unwrap();
        let Effect::Start {
            ticket: reserved, ..
        } = core.start(now()).unwrap().remove(0)
        else {
            panic!()
        };
        assert_eq!(reserved, ticket());
        let owned = PersistentChild::spawn(
            Path::new(env!("CARGO_BIN_EXE_node-guardian")),
            &worker_command(&root),
            &root.join("resources"),
            &root.join("state.db"),
            "persistent",
            ticket(),
        )
        .unwrap();
        core.started(&reserved, now().mono, now()).unwrap();
        let (guardian, worker) = owned.process_ids().unwrap();
        std::fs::write(root.join("ids"), format!("{guardian} {worker}")).unwrap();
        assert!(wait(
            || root.join("owner-exit").exists(),
            B::WorkerMaximumWall
        ));
        drop(owned);
    }
}
struct Fixture {
    root: PathBuf,
    owner: Option<Child>,
    ids: Option<(u32, u32)>,
}
impl Fixture {
    fn new(label: &str) -> Self {
        let root = std::env::temp_dir().join(format!("l6-guardian-{}-{label}", std::process::id()));
        std::fs::create_dir(&root).unwrap();
        Self {
            root,
            owner: None,
            ids: None,
        }
    }
    fn launch(&mut self) {
        self.owner = Some(command("owner", &self.root).spawn().unwrap());
        assert!(wait(
            || self.root.join("ids").exists() && self.root.join("worker-ready").exists(),
            B::WorkerStartup
        ));
        let ids: Vec<u32> = std::fs::read_to_string(self.root.join("ids"))
            .unwrap()
            .split_whitespace()
            .map(|p| p.parse().unwrap())
            .collect();
        self.ids = Some((ids[0], ids[1]));
    }
    fn kill_owner(&mut self) {
        let owner = self.owner.as_mut().unwrap();
        owner.kill().unwrap();
        owner.wait().unwrap();
        self.owner = None;
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        // Independent, owned fixture release contains REDs without signalling a saved PID.
        std::fs::write(self.root.join("fixture-exit"), b"exit").unwrap();
        if let Some(owner) = self.owner.as_mut() {
            if owner.try_wait().unwrap().is_none() {
                owner.kill().unwrap();
            }
            owner.wait().unwrap();
        }
        if let Some((guardian, worker)) = self.ids {
            assert!(
                wait(|| gone(guardian) && gone(worker), B::PreviousClose),
                "owned fixture processes remain"
            );
        }
        if let Ok(pid) = std::fs::read_to_string(self.root.join("descendant-pid")) {
            let pid = pid.parse().unwrap();
            assert!(
                wait(|| gone(pid), B::PreviousClose),
                "owned descendant remains"
            );
        }
        std::fs::remove_dir_all(&self.root).unwrap();
    }
}
struct Sibling(Child);
impl Sibling {
    fn new() -> Self {
        Self(
            Command::new("/bin/sleep")
                .arg(duration(B::MemberChosenWall).as_secs().to_string())
                .spawn()
                .unwrap(),
        )
    }
}
impl Drop for Sibling {
    fn drop(&mut self) {
        if self.0.try_wait().unwrap().is_none() {
            self.0.kill().unwrap();
        }
        self.0.wait().unwrap();
    }
}
#[test]
fn owner_death_automatically_terminates_owned_worker_without_signalling_unowned_pid() {
    let mut f = Fixture::new("death");
    let mut sibling = Sibling::new();
    f.launch();
    let mut recovered =
        guardian::recover(&f.root.join("state.db"), "persistent", &ticket()).unwrap();
    assert!(!recovered.observe_cleanup().unwrap().proven());
    assert!(matches!(
        recovered.request_stop(),
        Err(WorkerError::Refused(
            "guardian_lifetime_capability_not_owned"
        ))
    ));
    let mut stale = ticket();
    stale.run += 1;
    assert!(matches!(
        guardian::recover(&f.root.join("state.db"), "persistent", &stale),
        Err(WorkerError::Refused("worker_generation_stale"))
    ));
    f.kill_owner();
    let (mut restarted, _) = Core::open(&f.root.join("node.db"), binding(), caps(), now()).unwrap();
    assert_eq!(restarted.state().phase, RunPhase::CleanupUnproven);
    assert_eq!(restarted.state().workers.len(), 1);
    assert!(restarted.plan(&mut PlannerFixture, now()).is_err());
    let automatic = wait(
        || recovered.observe_cleanup().unwrap().proven(),
        B::PyMyceliumNodeProcessPhysicalnodeprocessInitShutdownTimeoutSecondsL1965,
    );
    // Baseline must be allowed to exit voluntarily before we assert the RED.
    if !automatic {
        std::fs::write(f.root.join("fixture-exit"), b"exit").unwrap();
    }
    assert!(wait(
        || recovered.observe_cleanup().unwrap().proven(),
        B::WorkerStartup
    ));
    let (guardian, worker) = f.ids.unwrap();
    assert!(wait(|| gone(guardian) && gone(worker), B::WorkerStartup));
    assert!(
        sibling.0.try_wait().unwrap().is_none(),
        "unowned sentinel was terminated"
    );
    assert!(!f.root.join("resources").exists());
    restarted
        .cleaned(&recovered.observe_cleanup().unwrap(), now())
        .unwrap();
    assert!(restarted.state().workers.is_empty());
    restarted.plan(&mut PlannerFixture, now()).unwrap();
    let Effect::Start { ticket: next, .. } = restarted.start(now()).unwrap().remove(0) else {
        panic!()
    };
    assert_ne!(next, ticket());
    assert!(
        restarted
            .cleaned(&recovered.observe_cleanup().unwrap(), now())
            .is_err()
    );
    assert!(
        automatic,
        "original owner died but its worker required manual fixture release"
    );
}
#[test]
fn persistent_guardian_stops_descendants_and_never_certifies_foreign_residue() {
    let mut f = Fixture::new("family");
    std::fs::write(f.root.join("family"), b"family").unwrap();
    f.launch();
    assert!(wait(
        || f.root.join("descendant-ready").exists(),
        B::WorkerStartup
    ));
    let descendant: u32 = std::fs::read_to_string(f.root.join("descendant-pid"))
        .unwrap()
        .parse()
        .unwrap();
    std::fs::write(f.root.join("resources").join("foreign"), b"keep").unwrap();
    f.kill_owner();
    let mut recovered =
        guardian::recover(&f.root.join("state.db"), "persistent", &ticket()).unwrap();
    assert!(wait(
        || matches!(
            recovered.observe_cleanup(),
            Err(WorkerError::Refused("guardian_cleanup_failed"))
        ),
        B::WorkerStartup
    ));
    let (guardian, worker) = f.ids.unwrap();
    assert!(wait(
        || gone(guardian) && gone(worker) && gone(descendant),
        B::WorkerStartup
    ));
    assert_eq!(
        std::fs::read(f.root.join("resources").join("foreign")).unwrap(),
        b"keep"
    );
    // A failed cleanup journal cannot be turned into proof by removing the residue later.
    std::fs::remove_file(f.root.join("resources").join("foreign")).unwrap();
    assert!(matches!(
        recovered.observe_cleanup(),
        Err(WorkerError::Refused("guardian_cleanup_failed"))
    ));
}
#[test]
fn failed_spawn_and_duplicate_journal_do_not_touch_foreign_resource_directory() {
    let f = Fixture::new("spawn-failure");
    std::fs::create_dir(f.root.join("resources")).unwrap();
    std::fs::write(f.root.join("resources").join("foreign"), b"keep").unwrap();
    let launch = || {
        PersistentChild::spawn(
            Path::new(env!("CARGO_BIN_EXE_node-guardian")),
            &worker_command(&f.root),
            &f.root.join("resources"),
            &f.root.join("state.db"),
            "persistent",
            ticket(),
        )
    };
    assert!(launch().is_err());
    assert!(launch().is_err());
    assert_eq!(
        std::fs::read(f.root.join("resources").join("foreign")).unwrap(),
        b"keep"
    );
    assert!(!f.root.join("worker-ready").exists());
}
#[test]
fn post_spawn_journal_failures_terminate_child_and_never_publish_cleanup_success() {
    for phase in ["Running", "Complete"] {
        let f = Fixture::new(phase);
        let db_path = f.root.join("state.db");
        drop(control_runtime::Store::open(&db_path).unwrap());
        let db = rusqlite::Connection::open(&db_path).unwrap();
        db.busy_timeout(Duration::ZERO).unwrap();
        db.execute_batch(&format!("CREATE TRIGGER reject_phase BEFORE UPDATE ON control_state WHEN new.name='persistent' AND instr(CAST(new.document AS TEXT),'\"{phase}\"')>0 BEGIN SELECT RAISE(ABORT,'injected phase publication'); END;")).unwrap();
        let launched = PersistentChild::spawn(
            Path::new(env!("CARGO_BIN_EXE_node-guardian")),
            &worker_command(&f.root),
            &f.root.join("resources"),
            &db_path,
            "persistent",
            ticket(),
        );
        if phase == "Running" {
            assert!(launched.is_err());
        } else {
            let mut owned = Supervisor::new(ticket(), launched.unwrap());
            owned.request_stop().unwrap();
            assert!(wait(
                || matches!(
                    owned.observe_cleanup(),
                    Err(WorkerError::Refused("guardian_cleanup_failed"))
                ),
                B::WorkerStartup
            ));
            drop(owned);
        }
        let store = control_runtime::Store::open(&db_path).unwrap();
        let group: serde_json::Value =
            serde_json::from_slice(&store.load_document("persistent/group").unwrap().unwrap().1)
                .unwrap();
        let pid = group["group"].as_u64().unwrap() as u32;
        let automatic = wait(
            || gone(pid),
            B::PyMyceliumNodeProcessPhysicalnodeprocessInitShutdownTimeoutSecondsL1965,
        );
        if !automatic {
            std::fs::write(f.root.join("fixture-exit"), b"exit").unwrap();
        }
        assert!(wait(|| gone(pid), B::WorkerStartup));
        let mut recovered = guardian::recover(&db_path, "persistent", &ticket()).unwrap();
        assert!(matches!(
            recovered.observe_cleanup(),
            Err(WorkerError::Refused("guardian_cleanup_failed"))
        ));
        assert!(automatic, "post-spawn persistence error abandoned a child");
    }
}
#[test]
fn explicit_stop_proof_survives_reopen_and_refuses_replaced_resources() {
    let f = Fixture::new("stop");
    let child = PersistentChild::spawn(
        Path::new(env!("CARGO_BIN_EXE_node-guardian")),
        &worker_command(&f.root),
        &f.root.join("resources"),
        &f.root.join("state.db"),
        "persistent",
        ticket(),
    )
    .unwrap();
    let ids = child.process_ids().unwrap();
    let mut owned = Supervisor::new(ticket(), child);
    owned.request_stop().unwrap();
    let stopped = wait(
        || owned.observe_cleanup().unwrap().proven(),
        B::PyMyceliumNodeProcessPhysicalnodeprocessInitShutdownTimeoutSecondsL1965,
    );
    if !stopped {
        std::fs::write(f.root.join("fixture-exit"), b"exit").unwrap();
    }
    assert!(wait(
        || owned.observe_cleanup().unwrap().proven(),
        B::WorkerStartup
    ));
    drop(owned);
    assert!(gone(ids.0) && gone(ids.1));
    let mut fresh = guardian::recover(&f.root.join("state.db"), "persistent", &ticket()).unwrap();
    assert!(fresh.observe_cleanup().unwrap().proven());
    std::fs::create_dir(f.root.join("resources")).unwrap();
    assert!(matches!(
        fresh.observe_cleanup(),
        Err(WorkerError::Refused("worker_resource_identity_changed"))
    ));
    assert!(stopped, "explicit Stop did not terminate the owned worker");
}
