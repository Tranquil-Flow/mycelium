// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{BudgetId as B, Bytes, ClockDomain, Entries, MonoInstant, Span, WallExpiry};
use node::*;
use std::{
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l6-core-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::create_dir(&p).unwrap();
        Self(p)
    }
    fn db(&self) -> PathBuf {
        self.0.join("node.db")
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
fn ms(b: B) -> u64 {
    b.span(Entries::ZERO).unwrap().millis()
}
fn now(t: u64) -> Now {
    Now {
        mono: MonoInstant::new(ClockDomain(7), t),
        wall: WallExpiry::from_unix_millis(t),
    }
}
fn owner() -> Binding {
    Binding {
        swarm_id: "swarm-fixture".into(),
        node_id: "owner".into(),
        seed_key_digest: "fixture-seed".into(),
    }
}
fn caps(owner: bool) -> Caps {
    Caps {
        storage_bytes: if owner {
            B::FreshStorageConsent
        } else {
            B::SavedStorageConsent
        }
        .bytes()
        .unwrap()
        .get(),
        memory_bytes: B::SavedStorageConsent.bytes().unwrap().get(),
        wall_ms: ms(if owner {
            B::WorkerDefaultWall
        } else {
            B::MemberChosenWall
        }),
        commands: B::ReplayKeep.count().unwrap().get(),
    }
}
fn bounds() -> ConsentBounds {
    ConsentBounds {
        minimum_storage: Bytes::ZERO,
        policy_storage: B::FreshStorageConsent.bytes().unwrap(),
        minimum_memory: Bytes::ZERO,
        maximum_memory: B::FreshStorageConsent.bytes().unwrap(),
        device_memory_ceiling: B::FreshStorageConsent.bytes().unwrap(),
        minimum_wall: Span::ZERO,
        maximum_wall: B::WorkerMaximumWall.span(Entries::ZERO).unwrap(),
        maximum_commands: B::ReplayCapacity.count().unwrap(),
    }
}
fn member() -> control::MemberBinding {
    control::MemberBinding {
        target_node_id: "member".into(),
        target_endpoint_id: "endpoint-member".into(),
        target_key_digest: "fixture-key".into(),
        membership_generation: "2".into(),
        member_incarnation: "incarnation-2".into(),
        run_id: "fixture".into(),
        deployment_id: "fixture".into(),
    }
}
fn grant() -> Consent {
    Consent::record(
        true,
        &disclosure_digest(),
        caps(false),
        Binding {
            node_id: "member".into(),
            ..owner()
        },
        &bounds(),
        0,
    )
    .unwrap()
}
/// Deliberately synthetic L7 adapter, not real placement or latency qualification.
struct StubPlanner;
#[test]
fn signed_mailbox_and_real_core_commit_once_with_one_locked_clock_and_no_partial_effects() {
    use control_runtime::{
        Store,
        mailbox::{Identity, Limits, Mailbox},
    };
    use serde_json::{Value, json};
    let t = Temp::new();
    let (mut core, _) = Core::open(&t.db(), owner(), caps(true), now(1_000_000)).unwrap();
    core.plan(&mut StubPlanner, now(1_000_000)).unwrap();
    core.start(now(1_000_000)).unwrap();
    let before = core.state().clone();
    let identity = Identity::from_secret("endpoint-owner", &[11; 32]).unwrap();
    let member_key = Identity::from_secret("endpoint-member", &[12; 32]).unwrap();
    let b = json!({"target_node_id":"member","target_endpoint_id":"endpoint-member","target_key_digest":member_key.verification_key_digest(),"membership_generation":1,"member_incarnation":"member-first","run_id":"run-1","deployment_id":"deployment-1"});
    let mut m = Mailbox::new(
        Store::open(&t.db()).unwrap(),
        Identity::from_secret("endpoint-owner", &[11; 32]).unwrap(),
        "swarm-fixture",
        "owner",
        Limits {
            pending: Entries::new(8),
            active_work: Entries::new(1),
            active_control: Entries::new(2),
        },
    )
    .unwrap();
    m.bind_member(&b, &member_key.public_key_record(), 1300.0)
        .unwrap();
    let mut statement = b.clone();
    for(k,v) in json!({"protocol":"mycelium.managed_control.owner_command.v1","owner_node_id":"owner","request_id":"cancel-1","sequence":1,"nonce":"cancel-nonce","issued_at":1000,"expires_at":1030,"swarm_id":"swarm-fixture","native_command":{"protocol":"mycelium.physical_node_control.v1","command_id":"cancel-command","command":"stop","run_id":"run-1","deployment_id":"deployment-1","payload":{}}}).as_object().unwrap() {statement[k]=v.clone();}
    let raw=serde_json::to_vec(&json!({"protocol":"mycelium.managed_control.signed_envelope.v1","statement":statement,"signature":identity.sign(&statement).unwrap(),"verification_key":identity.public_key_record()})).unwrap();
    let db = rusqlite::Connection::open(t.db()).unwrap();
    db.busy_timeout(std::time::Duration::ZERO).unwrap();
    db.execute_batch("CREATE TABLE fault_parent(id INTEGER PRIMARY KEY); CREATE TABLE fault_child(id INTEGER REFERENCES fault_parent(id) DEFERRABLE INITIALLY DEFERRED); CREATE TRIGGER fail_intent AFTER INSERT ON control_effects BEGIN INSERT INTO fault_child VALUES(99); END;").unwrap();
    assert!(
        core.dispatch_mailbox(
            &mut m,
            &raw,
            || now(1_001_000),
            || panic!(),
            Entries::new(8),
            |draft, e, n| {
                e.publish("forwarding", None, &json!({"cancelled":true}))?;
                draft.cancel(n)
            }
        )
        .is_err()
    );
    assert_eq!(core.state(), &before);
    let saved = Store::open(&t.db())
        .unwrap()
        .load_document("node")
        .unwrap()
        .unwrap();
    assert_eq!(serde_json::from_slice::<State>(&saved.1).unwrap(), before);
    for table in [
        "control_effects",
        "managed_auth_requests",
        "managed_commands",
    ] {
        assert_eq!(
            db.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r
                .get::<_, u64>(0))
                .unwrap(),
            0
        );
    }
    assert!(
        Store::open(&t.db())
            .unwrap()
            .load_document("forwarding")
            .unwrap()
            .is_none()
    );
    db.execute_batch("DROP TRIGGER fail_intent").unwrap();
    let mut clocks = 0;
    let reply = core
        .dispatch_mailbox(
            &mut m,
            &raw,
            || {
                clocks += 1;
                assert!(db.execute_batch("BEGIN IMMEDIATE").is_err());
                now(1_002_000)
            },
            || panic!(),
            Entries::new(8),
            |draft, e, n| {
                assert_eq!(e.now(), 1002.0);
                assert_eq!(n.mono.ticks(), 1_002_000);
                assert_eq!(e.statement()["native_command"]["command"], "stop");
                e.publish("forwarding", None, &json!({"cancelled":true}))?;
                draft.cancel(n)
            },
        )
        .unwrap();
    assert_eq!(clocks, 1);
    assert_eq!(core.state().phase, RunPhase::Stopping);
    assert_eq!(
        core.dispatch_mailbox(
            &mut m,
            &raw,
            || now(1_003_000),
            || panic!(),
            Entries::new(8),
            |_, _, _| panic!("cached replay must not transition")
        )
        .unwrap(),
        reply
    );
    let intents = m.pending_effects(Entries::new(8)).unwrap();
    assert_eq!(intents.len(), 1);
    assert!(matches!(
        serde_json::from_value::<Effect>(intents[0].document["effect"].clone()).unwrap(),
        Effect::Stop { .. }
    ));
    assert_eq!(intents[0].created_at, 1002.0);
    assert_eq!(
        Store::open(&t.db())
            .unwrap()
            .load_document("node")
            .unwrap()
            .unwrap()
            .0,
        saved.0 + 1
    );
    // The live handle adopts state only after COMMIT and can continue normally.
    core.tick(now(1_004_000)).unwrap();
    let stable = core.state().clone();
    let mut foreign = Mailbox::new(
        Store::open(&t.0.join("foreign.db")).unwrap(),
        Identity::from_secret("endpoint-owner", &[11; 32]).unwrap(),
        "swarm-fixture",
        "owner",
        Limits {
            pending: Entries::new(8),
            active_work: Entries::new(1),
            active_control: Entries::new(2),
        },
    )
    .unwrap();
    foreign
        .bind_member(&b, &member_key.public_key_record(), 1300.0)
        .unwrap();
    let error = core.dispatch_mailbox(
        &mut foreign,
        &raw,
        || now(1_005_000),
        || panic!(),
        Entries::new(8),
        |_, _, _| panic!("different database cannot transition"),
    );
    assert!(matches!(
        error,
        Err(Error::Refused("node_mailbox_database_mismatch"))
    ));
    assert_eq!(core.state(), &stable);
    let mut writer = Store::open(&t.db()).unwrap();
    let (revision, saved) = writer.load_document("node").unwrap().unwrap();
    writer
        .save_document("node", Some(revision), &saved)
        .unwrap();
    statement["sequence"] = 2.into();
    statement["request_id"] = "cancel-2".into();
    statement["nonce"] = "nonce-2".into();
    statement["native_command"]["command_id"] = "cancel-command-2".into();
    let second=serde_json::to_vec(&json!({"protocol":"mycelium.managed_control.signed_envelope.v1","statement":statement,"signature":identity.sign(&statement).unwrap(),"verification_key":identity.public_key_record()})).unwrap();
    assert!(matches!(
        core.dispatch_mailbox(
            &mut m,
            &second,
            || now(1_006_000),
            || panic!(),
            Entries::new(8),
            |_, _, _| panic!("stale core cannot transition")
        ),
        Err(Error::Refused("node_state_stale_writer"))
    ));
    assert_eq!(
        db.query_row("SELECT count(*) FROM managed_commands", [], |r| r
            .get::<_, u64>(0))
            .unwrap(),
        1
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&reply).unwrap()["statement"]["state"],
        "pending"
    );
}
#[test]
fn worker_crash_stops_cohort_and_late_failure_cannot_poison_successor() {
    let t = Temp::new();
    let mut c = joined(&t);
    let tickets = serving(&mut c, 0);
    let stops = c.worker_failed(&tickets[0], now(0)).unwrap();
    assert_eq!(stops.len(), 2);
    assert_eq!(c.state().workers.len(), 2);
    for ticket in &tickets {
        c.cleaned(&observed(ticket, true, true), now(0)).unwrap();
    }
    report(&mut c, 0);
    serving(&mut c, 0);
    let before = c.state().clone();
    assert!(c.worker_failed(&tickets[0], now(0)).is_err());
    assert_eq!(c.state(), &before);
    assert_eq!(
        c.state().last_failure.as_ref().unwrap().code,
        "worker_failed"
    );
}
#[test]
fn renewal_effects_are_cadenced_even_when_receipts_are_lost() {
    let t = Temp::new();
    let mut c = joined(&t);
    serving(&mut c, 0);
    let at = ms(B::LeaseRenewalInterval);
    assert_eq!(
        c.tick(now(at))
            .unwrap()
            .iter()
            .filter(|e| matches!(e, Effect::Renew { .. }))
            .count(),
        1
    );
    assert!(
        c.tick(now(at))
            .unwrap()
            .iter()
            .all(|e| !matches!(e, Effect::Renew { .. }))
    );
}
#[test]
fn unknown_disclosure_cannot_be_installed_as_standing_consent() {
    let t = Temp::new();
    let (mut c, _) = Core::open(&t.db(), owner(), caps(true), now(0)).unwrap();
    c.join(&member()).unwrap();
    let mut encoded = serde_json::to_value(grant()).unwrap();
    encoded["disclosure_digest"] = "sha256:wrong-disclosure".into();
    let forged = serde_json::from_value(encoded).unwrap();
    assert!(c.consent(forged).is_err());
}
#[test]
fn repeated_cancel_after_unproven_cleanup_cannot_extend_original_stop_expiry() {
    let t = Temp::new();
    let mut c = joined(&t);
    let tickets = serving(&mut c, 0);
    c.cancel(now(0)).unwrap();
    let before = c.state().workers[&tickets[0].node].stop_deadline;
    c.tick(now(ms(B::ObservedStop))).unwrap();
    c.cancel(now(ms(B::ObservedStop))).unwrap();
    assert_eq!(c.state().workers[&tickets[0].node].stop_deadline, before);
}
#[test]
fn restart_with_live_workers_retains_reservations_until_observed_cleanup_and_reuses_grant() {
    let t = Temp::new();
    let mut old = joined(&t);
    let tickets = serving(&mut old, 0);
    let (mut current, effects) = Core::open(&t.db(), owner(), caps(true), now(0)).unwrap();
    assert_eq!(effects.len(), 1);
    assert_eq!(current.state().workers.len(), 2);
    assert_eq!(current.state().phase, RunPhase::CleanupUnproven);
    assert!(current.started(&tickets[0], now(0).mono, now(0)).is_err());
    assert!(current.plan(&mut StubPlanner, now(0)).is_err());
    for ticket in &tickets {
        current
            .cleaned(&observed(ticket, true, true), now(0))
            .unwrap();
    }
    report(&mut current, 0);
    let newer = serving(&mut current, 0);
    assert_ne!(newer, tickets);
    assert!(
        current
            .cleaned(&observed(&tickets[0], true, true), now(0))
            .is_err()
    );
    assert_eq!(current.state().workers.len(), 2);
    assert_eq!(current.state().members["member"].consent, Some(grant()));
}
#[test]
fn f16_profile_stop_allowance_outlasts_injected_close_and_no_early_release() {
    struct Coherent;
    impl Planner for Coherent {
        fn plan(&mut self, m: &[PlanningMember]) -> Result<Plan> {
            let mut p = StubPlanner.plan(m)?;
            p.stop_ms = ms(B::ObservedStop) * B::P99Multiplier.count().unwrap().get();
            Ok(p)
        }
    }
    let t = Temp::new();
    let mut c = joined(&t);
    c.plan(&mut Coherent, now(0)).unwrap();
    let effects = c.start(now(0)).unwrap();
    let tickets: Vec<_> = effects
        .into_iter()
        .map(|e| match e {
            Effect::Start { ticket, .. } => ticket,
            _ => panic!(),
        })
        .collect();
    for ticket in &tickets {
        c.started(ticket, now(0).mono, now(0)).unwrap();
    }
    c.cancel(now(0)).unwrap();
    let mut q = sim::Scheduler::default();
    q.at(ms(B::HistoricalOuterStop), 0, false).unwrap();
    q.at(ms(B::ObservedStop), 0, true).unwrap();
    while let Some(e) = q.pop().unwrap() {
        if e.event {
            for ticket in &tickets {
                c.cleaned(&observed(ticket, true, true), now(e.at_ms))
                    .unwrap();
            }
        } else {
            c.tick(now(e.at_ms)).unwrap();
            assert_eq!(c.state().phase, RunPhase::Stopping, "F16 stop-before-close");
            assert_eq!(c.state().workers.len(), 2);
        }
    }
    assert_eq!(c.state().phase, RunPhase::Idle);
    assert!(c.state().last_failure.is_none());
}
#[test]
fn staggered_worker_receipts_keep_true_start_origins_not_arrival_times() {
    let t = Temp::new();
    let mut c = joined(&t);
    c.plan(&mut StubPlanner, now(0)).unwrap();
    let effects = c.start(now(0)).unwrap();
    let tickets: Vec<_> = effects
        .into_iter()
        .map(|e| match e {
            Effect::Start { ticket, .. } => ticket,
            _ => panic!(),
        })
        .collect();
    let owner = tickets.iter().find(|t| t.node == "owner").unwrap();
    let member = tickets.iter().find(|t| t.node == "member").unwrap();
    c.started(member, now(0).mono, now(ms(B::WorkerCleanupReserve)))
        .unwrap();
    c.started(
        owner,
        now(ms(B::WorkerCleanupReserve)).mono,
        now(ms(B::WorkerStartup)),
    )
    .unwrap();
    assert_eq!(c.state().workers["owner"].expires.unwrap().ticks, 1_810_000);
    assert_eq!(c.state().workers["member"].expires.unwrap().ticks, 600_000);
    assert_eq!(c.state().rollover_at.unwrap().ticks, 480_000);
}
#[test]
fn l2_scheduler_drives_production_sqlite_core_through_two_rollovers_and_restart() {
    #[derive(Clone, Debug)]
    enum Event {
        Start,
        Report,
        Tick,
        Clean,
        Restart,
        Cancel,
    }
    fn run(seed: u64) -> Vec<(u64, String, u64)> {
        let t = Temp::new();
        let mut c = joined(&t);
        let mut q = sim::Scheduler::default();
        let lead = B::EffectiveRolloverLead
            .wall_span(
                B::MemberChosenWall.span(Entries::ZERO).unwrap(),
                Entries::ZERO,
            )
            .unwrap()
            .millis();
        let interval = ms(B::MemberChosenWall) - lead;
        let close = ms(B::ObservedStop);
        let cycle = interval + close;
        for run in 0..3 {
            let base = run * cycle;
            if run == 2 {
                q.at(base, 0, Event::Restart).unwrap();
            }
            q.at(base, 1, Event::Report).unwrap();
            q.at(base, 2, Event::Start).unwrap();
            if run < 2 {
                for renew in 1..=4 {
                    let at = base + renew * ms(B::LeaseRenewalInterval);
                    let task = (sim::random(seed, run, renew, 0) % 2) as u32;
                    q.at(at, task, Event::Report).unwrap();
                    q.at(at, 1 - task, Event::Tick).unwrap();
                }
                q.at(base + interval, 0, Event::Tick).unwrap();
                q.at(base + cycle, 0, Event::Clean).unwrap();
            } else {
                q.at(base, 3, Event::Cancel).unwrap();
                q.at(base + close, 0, Event::Clean).unwrap();
            }
        }
        let mut trace = Vec::new();
        let mut starts = 0;
        let mut stops = 0;
        let mut restarts = 0;
        while let Some(event) = q.pop().unwrap() {
            let at = event.at_ms;
            match event.event {
                Event::Start => {
                    serving(&mut c, at);
                    starts += 1;
                    assert_eq!(
                        c.state().workers["member"].expires.unwrap().ticks,
                        at + 600_000
                    );
                }
                Event::Report => report(&mut c, at),
                Event::Tick => {
                    let effects = c.tick(now(at)).unwrap();
                    stops += effects
                        .iter()
                        .filter(|e| matches!(e, Effect::Stop { .. }))
                        .count();
                }
                Event::Cancel => {
                    c.cancel(now(at)).unwrap();
                }
                Event::Clean => {
                    let tickets: Vec<_> = c
                        .state()
                        .workers
                        .values()
                        .map(|w| w.ticket.clone())
                        .collect();
                    assert_eq!(tickets.len(), 2);
                    for ticket in tickets {
                        c.cleaned(&observed(&ticket, true, true), now(at)).unwrap();
                    }
                    assert_eq!(c.state().phase, RunPhase::Idle);
                }
                Event::Restart => {
                    let (next, effects) =
                        Core::open(&t.db(), owner(), caps(true), now(at)).unwrap();
                    assert_eq!(effects.len(), 1);
                    c = next;
                    restarts += 1;
                }
            }
            trace.push((at, format!("{:?}", c.state().phase), c.state().run));
        }
        assert_eq!((starts, stops, restarts), (3, 4, 1));
        assert!(c.state().workers.is_empty());
        assert!(q.pending() == 0);
        trace
    }
    for seed in [17, 89] {
        assert_eq!(run(seed), run(seed));
    }
}
#[test]
fn expired_lease_at_late_start_receipt_cannot_enter_serving() {
    let t = Temp::new();
    let mut c = joined(&t);
    c.plan(&mut StubPlanner, now(0)).unwrap();
    let effects = c.start(now(0)).unwrap();
    let ticket = effects
        .iter()
        .find_map(|e| match e {
            Effect::Start { ticket, .. } if ticket.node == "member" => Some(ticket),
            _ => None,
        })
        .unwrap();
    assert!(
        c.started(ticket, now(0).mono, now(ms(B::MembershipTtl)))
            .is_err()
    );
    assert_ne!(c.state().phase, RunPhase::Serving);
    assert_eq!(c.state().workers.len(), 2);
}
#[test]
fn stop_timeout_survives_later_cleanup_as_an_explicit_diagnostic() {
    let t = Temp::new();
    let mut c = joined(&t);
    let tickets = serving(&mut c, 0);
    c.cancel(now(0)).unwrap();
    c.tick(now(ms(B::ObservedStop))).unwrap();
    assert_eq!(c.state().phase, RunPhase::CleanupUnproven);
    for ticket in tickets {
        c.cleaned(&observed(&ticket, true, true), now(ms(B::ObservedStop)))
            .unwrap();
    }
    assert_eq!(
        c.state().last_failure.as_ref().unwrap().code,
        "worker_stop_timeout"
    );
}
#[test]
fn sqlite_failed_start_commit_emits_no_effect_and_keeps_state() {
    let t = Temp::new();
    let mut c = joined(&t);
    c.plan(&mut StubPlanner, now(0)).unwrap();
    let before = c.state().clone();
    let db = rusqlite::Connection::open(t.db()).unwrap();
    db.execute_batch("CREATE TRIGGER refuse_state BEFORE UPDATE ON control_state BEGIN SELECT RAISE(ABORT,'injected_state_write_failure'); END;").unwrap();
    assert!(c.start(now(0)).is_err());
    assert_eq!(c.state(), &before);
    db.execute_batch("DROP TRIGGER refuse_state").unwrap();
    assert_eq!(c.start(now(0)).unwrap().len(), 2);
}
#[test]
fn same_expiry_wall_rollback_cannot_extend_pin_and_reboot_needs_newer_lease() {
    let t = Temp::new();
    let mut c = joined(&t);
    let initial = c.state().members["member"].lease.clone().unwrap();
    let shifted = Now {
        mono: now(ms(B::LeaseRenewalInterval)).mono,
        wall: WallExpiry::from_unix_millis(0),
    };
    c.report(
        "member",
        "2",
        c.state().epoch,
        WallExpiry::from_unix_millis(ms(B::MembershipTtl)),
        shifted,
    )
    .unwrap();
    assert_eq!(
        c.state().members["member"].lease.as_ref().unwrap().deadline,
        initial.deadline
    );
    drop(c);
    let reboot = Now {
        mono: MonoInstant::new(ClockDomain(8), 0),
        wall: WallExpiry::from_unix_millis(0),
    };
    let (mut c, _) = Core::open(&t.db(), owner(), caps(true), reboot).unwrap();
    assert!(
        c.report(
            "member",
            "2",
            c.state().epoch,
            WallExpiry::from_unix_millis(ms(B::MembershipTtl)),
            reboot
        )
        .is_err()
    );
}
#[test]
fn planner_adapter_cannot_add_members_or_enlarge_consent_and_too_slow_refuses() {
    struct Bad(bool);
    impl Planner for Bad {
        fn plan(&mut self, m: &[PlanningMember]) -> Result<Plan> {
            let mut p = StubPlanner.plan(m)?;
            if self.0 {
                p.assignments.get_mut("member").unwrap().storage_bytes += 1;
            } else {
                p.stop_ms = ms(B::ObservedStop) * 3;
            }
            Ok(p)
        }
    }
    let t = Temp::new();
    let mut c = joined(&t);
    let before = c.state().clone();
    assert!(c.plan(&mut Bad(true), now(0)).is_err());
    assert_eq!(c.state(), &before);
    assert!(c.plan(&mut Bad(false), now(0)).is_err());
    assert_eq!(c.state(), &before);
}
impl Planner for StubPlanner {
    fn plan(&mut self, m: &[PlanningMember]) -> Result<Plan> {
        Ok(Plan {
            assignments: m.iter().map(|m| (m.node.clone(), m.caps.clone())).collect(),
            stop_ms: ms(B::ObservedStop),
            start_ms: ms(B::WorkerStartup),
            safety_ms: ms(B::WorkerCleanupReserve),
        })
    }
}
fn joined(t: &Temp) -> Core {
    let (mut c, e) = Core::open(&t.db(), owner(), caps(true), now(0)).unwrap();
    assert!(e.is_empty());
    c.join(&member()).unwrap();
    c.consent(grant()).unwrap();
    report(&mut c, 0);
    c
}
struct FakeWorker(Residue);
impl WorkerAdapter for FakeWorker {
    fn request_stop(&mut self) -> std::result::Result<(), WorkerError> {
        Ok(())
    }
    fn observe(&mut self) -> std::result::Result<Residue, WorkerError> {
        Ok(self.0)
    }
}
fn observed(ticket: &Ticket, process_gone: bool, resources_gone: bool) -> CleanupObservation {
    Supervisor::new(
        ticket.clone(),
        FakeWorker(Residue {
            process_gone,
            resources_gone,
        }),
    )
    .observe_cleanup()
    .unwrap()
}
fn report(c: &mut Core, t: u64) {
    c.report(
        "member",
        "2",
        c.state().epoch,
        WallExpiry::from_unix_millis(t + ms(B::MembershipTtl)),
        now(t),
    )
    .unwrap();
}
fn serving(c: &mut Core, t: u64) -> Vec<Ticket> {
    c.plan(&mut StubPlanner, now(t)).unwrap();
    let starts = c.start(now(t)).unwrap();
    let tickets: Vec<_> = starts
        .into_iter()
        .map(|e| match e {
            Effect::Start { ticket, .. } => ticket,
            _ => panic!("unexpected effect"),
        })
        .collect();
    for ticket in &tickets {
        c.started(ticket, now(t).mono, now(t)).unwrap();
    }
    assert_eq!(c.state().phase, RunPhase::Serving);
    tickets
}
#[test]
fn f15b_owner_local_policy_is_not_a_standing_grant_and_member_uses_chosen_cap() {
    let t = Temp::new();
    let mut c = joined(&t);
    serving(&mut c, 0);
    let s = c.state();
    assert!(!s.members.contains_key("owner"));
    assert_eq!(s.workers["owner"].authority, AuthoritySource::OwnerLocal);
    assert_eq!(s.workers["owner"].expires.unwrap().ticks, 1_800_000);
    assert_eq!(s.workers["member"].expires.unwrap().ticks, 600_000);
}
#[test]
fn f1_renewal_refreshes_pin_but_never_extends_worker_window() {
    let t = Temp::new();
    let mut c = joined(&t);
    serving(&mut c, 0);
    let initial = c.state().workers["member"].expires;
    let due = ms(B::LeaseRenewalInterval);
    let effects = c.tick(now(due)).unwrap();
    assert!(
        effects
            .iter()
            .any(|e| matches!(e,Effect::Renew{node,..}if node=="member"))
    );
    report(&mut c, due);
    report(&mut c, due * 2);
    report(&mut c, due * 3);
    assert_eq!(c.state().workers["member"].expires, initial);
    assert_eq!(c.state().phase, RunPhase::Serving);
}
#[test]
fn f4_f15_two_rollovers_keep_independent_absolute_origins() {
    let t = Temp::new();
    let mut c = joined(&t);
    let mut start = 0;
    for run in 1..=3 {
        let tickets = serving(&mut c, start);
        assert_eq!(c.state().run, run);
        assert_eq!(c.state().rollover_at.unwrap().ticks, start + 480_000);
        let rollover = start + 480_000;
        for offset in [100_000, 200_000, 300_000, 400_000] {
            report(&mut c, start + offset);
        }
        let effects = c.tick(now(rollover)).unwrap();
        assert_eq!(
            effects
                .iter()
                .filter(|e| matches!(e, Effect::Stop { .. }))
                .count(),
            2
        );
        for ticket in tickets {
            c.cleaned(
                &observed(&ticket, true, true),
                now(rollover + ms(B::ObservedStop)),
            )
            .unwrap();
        }
        assert!(c.state().workers.is_empty());
        start = rollover + ms(B::ObservedStop);
        report(&mut c, start);
    }
}
#[test]
fn f16_unproven_cleanup_retains_reservation_and_blocks_successor() {
    let t = Temp::new();
    let mut c = joined(&t);
    let tickets = serving(&mut c, 0);
    c.cancel(now(0)).unwrap();
    assert!(
        c.cleaned(
            &observed(&tickets[0], false, true),
            now(ms(B::ObservedStop))
        )
        .is_err()
    );
    assert_eq!(c.state().workers.len(), 2);
    assert!(c.plan(&mut StubPlanner, now(ms(B::ObservedStop))).is_err());
    assert_eq!(c.state().phase, RunPhase::CleanupUnproven);
    for ticket in tickets {
        c.cleaned(&observed(&ticket, true, true), now(ms(B::ObservedStop)))
            .unwrap();
    }
    assert_eq!(c.state().phase, RunPhase::Idle);
}
#[test]
fn f3_owner_restart_preserves_join_and_consent_requires_report_and_fences_old_writer() {
    let t = Temp::new();
    let mut old = joined(&t);
    let epoch = old.state().epoch;
    let (mut new, effects) = Core::open(&t.db(), owner(), caps(true), now(0)).unwrap();
    assert_eq!(new.state().members["member"].consent, Some(grant()));
    assert_eq!(new.state().epoch, epoch + 1);
    assert_eq!(
        effects,
        vec![Effect::RefreshReport {
            node: "member".into(),
            epoch: epoch + 1
        }]
    );
    assert!(new.plan(&mut StubPlanner, now(0)).is_err());
    assert!(old.plan(&mut StubPlanner, now(0)).is_err());
    assert!(
        new.report(
            "member",
            "1",
            new.state().epoch,
            WallExpiry::from_unix_millis(ms(B::MembershipTtl)),
            now(0)
        )
        .is_err()
    );
    report(&mut new, 0);
    serving(&mut new, 0);
}
#[test]
fn consent_withdrawal_revokes_before_cleanup_and_never_fabricates_owner_consent() {
    let t = Temp::new();
    let mut c = joined(&t);
    serving(&mut c, 0);
    let effects = c.withdraw("member", now(0)).unwrap();
    assert!(effects.iter().any(|e| matches!(e, Effect::Stop { .. })));
    assert!(
        c.state().members["member"]
            .consent
            .as_ref()
            .unwrap()
            .withdrawn_at
            .is_some()
    );
    assert!(c.start(now(0)).is_err());
}
