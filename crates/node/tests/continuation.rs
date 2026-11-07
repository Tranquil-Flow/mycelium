// SPDX-License-Identifier: AGPL-3.0-only
// Oracle: demo_chain.py:1117-1141; owner_run.py:351-377 @ f4e366e9.
// BACKLOG 1 / V03-PLAN lane 3: lost descriptor is fatal, NOT retried in v0.2.
use budgets::{BudgetId as B, Bytes, ClockDomain, Entries, MonoInstant, Span, WallExpiry};
use node::*;
use std::{
    path::PathBuf,
    process::Command,
    time::{Duration, Instant as RealInstant},
};
fn ms(b: B) -> u64 {
    b.span(Entries::ZERO).unwrap().millis()
}
fn now(t: u64) -> Now {
    Now {
        mono: MonoInstant::new(ClockDomain(1), t),
        wall: WallExpiry::from_unix_millis(t),
    }
}
fn caps(owner: bool) -> Caps {
    Caps {
        storage_bytes: B::SavedStorageConsent.bytes().unwrap().get(),
        memory_bytes: B::SavedStorageConsent.bytes().unwrap().get(),
        wall_ms: ms(if owner {
            B::WorkerDefaultWall
        } else {
            B::MemberChosenWall
        }),
        commands: B::ReplayKeep.count().unwrap().get(),
    }
}
fn member() -> control::MemberBinding {
    control::MemberBinding {
        target_node_id: "member".into(),
        target_endpoint_id: "endpoint".into(),
        target_key_digest: "key".into(),
        membership_generation: "1".into(),
        member_incarnation: "first".into(),
        run_id: "run".into(),
        deployment_id: "deployment".into(),
    }
}
struct Fixture {
    root: PathBuf,
    core: Core,
}
impl Fixture {
    fn new(label: &str) -> Self {
        let root =
            std::env::temp_dir().join(format!("l6-continuation-{}-{label}", std::process::id()));
        std::fs::create_dir(&root).unwrap();
        let owner = Binding {
            node_id: "owner".into(),
            swarm_id: "swarm".into(),
            seed_key_digest: "seed".into(),
        };
        let (mut core, _) =
            Core::open(&root.join("state.db"), owner.clone(), caps(true), now(0)).unwrap();
        core.join(&member()).unwrap();
        core.consent(
            Consent::record(
                true,
                &disclosure_digest(),
                caps(false),
                Binding {
                    node_id: "member".into(),
                    ..owner
                },
                &ConsentBounds {
                    minimum_storage: Bytes::ZERO,
                    policy_storage: B::FreshStorageConsent.bytes().unwrap(),
                    minimum_memory: Bytes::ZERO,
                    maximum_memory: B::FreshStorageConsent.bytes().unwrap(),
                    device_memory_ceiling: B::FreshStorageConsent.bytes().unwrap(),
                    minimum_wall: Span::ZERO,
                    maximum_wall: B::WorkerMaximumWall.span(Entries::ZERO).unwrap(),
                    maximum_commands: B::ReplayCapacity.count().unwrap(),
                },
                0,
            )
            .unwrap(),
        )
        .unwrap();
        refresh(&mut core, 0);
        Self { root, core }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.root).unwrap();
    }
}
// Development planner only: these are registry-backed phase inputs, not measured p99.
struct PlannerFixture;
impl Planner for PlannerFixture {
    fn plan(&mut self, m: &[PlanningMember]) -> Result<Plan> {
        Ok(Plan {
            assignments: m.iter().map(|m| (m.node.clone(), m.caps.clone())).collect(),
            stop_ms: ms(B::ObservedStop),
            start_ms: ms(B::WorkerStartup),
            safety_ms: ms(B::WorkerCleanupReserve),
        })
    }
}
fn refresh(c: &mut Core, at: u64) {
    c.report(
        "member",
        "1",
        c.state().epoch,
        WallExpiry::from_unix_millis(at + ms(B::MembershipTtl)),
        now(at),
    )
    .unwrap();
}
fn start(c: &mut Core, at: u64) -> Vec<Ticket> {
    c.plan(&mut PlannerFixture, now(at)).unwrap();
    c.start(now(at))
        .unwrap()
        .into_iter()
        .map(|e| {
            let Effect::Start { ticket, .. } = e else {
                panic!()
            };
            c.started(&ticket, now(at).mono, now(at)).unwrap();
            ticket
        })
        .collect()
}
struct Absent;
impl WorkerAdapter for Absent {
    fn request_stop(&mut self) -> std::result::Result<(), WorkerError> {
        Ok(())
    }
    fn observe(&mut self) -> std::result::Result<Residue, WorkerError> {
        Ok(Residue {
            process_gone: true,
            resources_gone: true,
        })
    }
}
fn retire(c: &mut Core, tickets: &[Ticket], at: u64) {
    for t in tickets {
        c.cleaned(
            &Supervisor::new(t.clone(), Absent)
                .observe_cleanup()
                .unwrap(),
            now(at),
        )
        .unwrap();
    }
}
fn rollover(c: &mut Core) -> u64 {
    let at = c.state().rollover_at.unwrap().ticks;
    assert_eq!(at, 480_000); // Independent v0.1.4 600s member minus 120s margin.
    let mut renewal = ms(B::LeaseRenewalInterval);
    while renewal < at {
        refresh(c, renewal);
        renewal += ms(B::LeaseRenewalInterval);
    }
    refresh(c, at - 1);
    assert!(
        c.tick(now(at - 1))
            .unwrap()
            .iter()
            .all(|e| !matches!(e, Effect::Stop { .. }))
    );
    assert_eq!(
        c.tick(now(at))
            .unwrap()
            .iter()
            .filter(|e| matches!(e, Effect::Stop { .. }))
            .count(),
        2
    );
    at
}
#[test]
fn v03_lane3_lost_descriptor_rollover_members_changed_is_terminal_without_retry() {
    let mut f = Fixture::new("lost");
    let tickets = start(&mut f.core, 0);
    let at = rollover(&mut f.core);
    retire(&mut f.core, &tickets, at);
    let result = f.core.continue_rollover(&[], &mut PlannerFixture, now(at));
    assert!(matches!(
        result,
        Err(Error::Refused("rollover_members_changed"))
    ));
    assert_eq!(
        f.core.state().last_failure.as_ref().unwrap().code,
        "rollover_members_changed"
    );
    let reports = [RolloverReport {
        binding: member(),
        expiry: WallExpiry::from_unix_millis(at + ms(B::MembershipTtl)),
    }];
    assert!(matches!(
        f.core
            .continue_rollover(&reports, &mut PlannerFixture, now(at)),
        Err(Error::Refused("rollover_not_pending"))
    ));
    assert!(
        f.core
            .tick(now(at))
            .unwrap()
            .iter()
            .all(|e| !matches!(e, Effect::Start { .. }))
    );
    assert_eq!(f.core.state().run, 1);
}
#[test]
fn wall_timed_rollover_then_stop_requires_real_child_cleanup_proof() {
    let mut f = Fixture::new("native");
    let tickets = start(&mut f.core, 0);
    let mut workers: Vec<_> = tickets
        .iter()
        .map(|ticket| {
            let root = f.root.join(format!("worker-{}", ticket.node));
            let mut command = Command::new("/bin/sleep");
            command.arg((ms(B::MemberChosenWall) / 1000).to_string());
            Supervisor::new(
                ticket.clone(),
                OwnedNative(process::NativeChild::spawn(&mut command, &root).unwrap()),
            )
        })
        .collect();
    let at = rollover(&mut f.core);
    for w in &mut workers {
        w.request_stop().unwrap();
    }
    let reports = [RolloverReport {
        binding: member(),
        expiry: WallExpiry::from_unix_millis(at + ms(B::MembershipTtl)),
    }];
    assert!(matches!(
        f.core
            .continue_rollover(&reports, &mut PlannerFixture, now(at)),
        Err(Error::Refused("rollover_cleanup_unproven"))
    ));
    for w in &mut workers {
        clean(&mut f.core, w, at);
    }
    let effects = f
        .core
        .continue_rollover(&reports, &mut PlannerFixture, now(at))
        .unwrap();
    assert_eq!(effects.len(), 2);
    let mut successors: Vec<_> = effects
        .into_iter()
        .map(|e| {
            let Effect::Start { ticket, .. } = e else {
                panic!()
            };
            assert_eq!(ticket.run, 2);
            let mut command = Command::new("/bin/sleep");
            command.arg((ms(B::MemberChosenWall) / 1000).to_string());
            let child = process::NativeChild::spawn(
                &mut command,
                &f.root.join(format!("next-{}", ticket.node)),
            )
            .unwrap();
            f.core.started(&ticket, now(at).mono, now(at)).unwrap();
            Supervisor::new(ticket, OwnedNative(child))
        })
        .collect();
    let stop = f.core.request_stop(now(at)).unwrap();
    assert_eq!(stop.effects.len(), 2);
    assert!(matches!(
        f.core.finish_stop(&stop.fence),
        Err(Error::Refused("cleanup_unproven"))
    ));
    for w in &mut successors {
        w.request_stop().unwrap();
        clean(&mut f.core, w, at);
    }
    let proof = f.core.finish_stop(&stop.fence).unwrap();
    assert_eq!(proof.run(), 2);
    assert_eq!(proof.epoch(), 1);
    assert!(f.core.state().workers.is_empty());
    assert_eq!(f.core.state().phase, RunPhase::Idle);
    refresh(&mut f.core, at);
    start(&mut f.core, at);
    assert!(f.core.finish_stop(&stop.fence).is_err());
    // Final run has no spawned native workers; retire its logical reservations.
    let final_stop = f.core.request_stop(now(at)).unwrap();
    let ts: Vec<_> = final_stop
        .effects
        .into_iter()
        .map(|e| {
            let Effect::Stop { ticket, .. } = e else {
                panic!()
            };
            ticket
        })
        .collect();
    retire(&mut f.core, &ts, at);
}
#[test]
fn descriptor_identity_changes_are_fatal_but_renewals_during_drain_are_not() {
    for field in [
        "endpoint",
        "key",
        "generation",
        "incarnation",
        "duplicate",
        "healthy",
    ] {
        let mut f = Fixture::new(field);
        let tickets = start(&mut f.core, 0);
        let at = rollover(&mut f.core);
        refresh(&mut f.core, at);
        retire(&mut f.core, &tickets, at);
        let mut b = member();
        match field {
            "endpoint" => b.target_endpoint_id = "other".into(),
            "key" => b.target_key_digest = "other".into(),
            "generation" => b.membership_generation = "other".into(),
            "incarnation" => b.member_incarnation = "other".into(),
            _ => {}
        }
        let mut reports = vec![RolloverReport {
            binding: b,
            expiry: WallExpiry::from_unix_millis(at + ms(B::MembershipTtl)),
        }];
        if field == "duplicate" {
            reports.push(RolloverReport {
                binding: member(),
                expiry: reports[0].expiry,
            });
        }
        let result = f
            .core
            .continue_rollover(&reports, &mut PlannerFixture, now(at));
        if field == "healthy" {
            assert_eq!(result.unwrap().len(), 2);
        } else {
            assert!(matches!(
                result,
                Err(Error::Refused("rollover_members_changed"))
            ));
        }
    }
}
#[test]
fn stop_revokes_pending_rollover_and_fence_cannot_cross_owner_or_restart() {
    let mut f = Fixture::new("cancel");
    let tickets = start(&mut f.core, 0);
    let at = rollover(&mut f.core);
    let stop = f.core.request_stop(now(at)).unwrap();
    retire(&mut f.core, &tickets, at);
    assert!(matches!(
        f.core.continue_rollover(&[], &mut PlannerFixture, now(at)),
        Err(Error::Refused("rollover_not_pending"))
    ));
    f.core.finish_stop(&stop.fence).unwrap();
    let other_binding = Binding {
        node_id: "other".into(),
        ..f.core.state().owner.clone()
    };
    let (mut other, _) =
        Core::open(&f.root.join("other.db"), other_binding, caps(true), now(at)).unwrap();
    let other_tickets = start(&mut other, at);
    other.request_stop(now(at)).unwrap();
    retire(&mut other, &other_tickets, at);
    assert_eq!(other.state().run, f.core.state().run);
    assert!(other.finish_stop(&stop.fence).is_err());
    let (reopened, _) = Core::open(
        &f.root.join("state.db"),
        f.core.state().owner.clone(),
        caps(true),
        now(at),
    )
    .unwrap();
    assert!(reopened.finish_stop(&stop.fence).is_err());
    assert!(
        matches!(
            f.core.finish_stop(&stop.fence),
            Err(Error::Refused("node_state_stale_writer"))
        ),
        "a stale owner cannot issue fresh cleanup proof"
    );
}
#[test]
fn continuation_commit_failure_does_not_publish_partial_plan_or_start() {
    let mut f = Fixture::new("atomic");
    let tickets = start(&mut f.core, 0);
    let at = rollover(&mut f.core);
    retire(&mut f.core, &tickets, at);
    let before = f.core.state().clone();
    let db = rusqlite::Connection::open(f.root.join("state.db")).unwrap();
    db.busy_timeout(Duration::ZERO).unwrap();
    db.execute_batch("CREATE TRIGGER deny_node_update BEFORE UPDATE ON control_state BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    let reports = [RolloverReport {
        binding: member(),
        expiry: WallExpiry::from_unix_millis(at + ms(B::MembershipTtl)),
    }];
    assert!(
        f.core
            .continue_rollover(&reports, &mut PlannerFixture, now(at))
            .is_err()
    );
    assert_eq!(f.core.state(), &before);
    db.execute_batch("DROP TRIGGER deny_node_update").unwrap();
    assert_eq!(
        f.core
            .continue_rollover(&reports, &mut PlannerFixture, now(at))
            .unwrap()
            .len(),
        2
    );
}
struct OwnedNative(process::NativeChild);
impl WorkerAdapter for OwnedNative {
    fn request_stop(&mut self) -> std::result::Result<(), WorkerError> {
        self.0.request_stop()
    }
    fn observe(&mut self) -> std::result::Result<Residue, WorkerError> {
        self.0.observe()
    }
}
impl Drop for OwnedNative {
    fn drop(&mut self) {
        self.0.request_stop().expect("fixture exact-child stop");
        let end = RealInstant::now() + Duration::from_millis(ms(B::WorkerStartup));
        loop {
            if self
                .0
                .observe()
                .expect("fixture exact-child observation")
                .process_gone
            {
                break;
            }
            assert!(RealInstant::now() < end, "fixture child cleanup timed out");
            std::thread::yield_now();
        }
    }
}
fn clean(c: &mut Core, w: &mut Supervisor<OwnedNative>, at: u64) {
    let deadline = RealInstant::now() + Duration::from_millis(ms(B::WorkerStartup));
    loop {
        let observation = w.observe_cleanup().unwrap();
        if observation.proven() {
            c.cleaned(&observation, now(at)).unwrap();
            break;
        }
        assert!(RealInstant::now() < deadline);
        std::thread::yield_now();
    }
}
