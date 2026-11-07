// SPDX-License-Identifier: AGPL-3.0-only
#[path = "support/http_composition.rs"]
mod http_composition;
#[path = "../../artifacts/tests/support/temp.rs"]
mod test_temp;
use budgets::{BudgetId as B, Bytes, ClockDomain, Entries, MonoInstant, Span, WallExpiry};
use planner::{admission::*, control_adapter::*, discovery::*, placement};
use protocol::identity::Identity;
use serde_json::{Value, json};
use std::path::PathBuf;
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = test_temp::directory("control");
        Self(p)
    }
    fn db(&self) -> PathBuf {
        self.0.join("state.db")
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
fn span(b: B) -> Span {
    b.span(Entries::ZERO).unwrap()
}
fn now(t: u64) -> node::Now {
    node::Now {
        mono: MonoInstant::new(ClockDomain(7), t),
        wall: WallExpiry::from_unix_millis(t),
    }
}
fn fixture() -> (Value, Value) {
    let f: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    let c = f["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|x| x["legacy_saved_selection"] == true && x.get("expected").is_some())
        .unwrap()
        .clone();
    (f, c)
}
fn input<'a>(f: &'a Value, c: &'a Value) -> PlanInputs<'a> {
    PlanInputs {
        model: f["models"]
            .as_array()
            .unwrap()
            .iter()
            .find(|m| m["model_id"] == c["model_id"])
            .unwrap(),
        policy: &f["policies"]["int8-weight-only"],
        resources: &f["resources"],
        request: c,
    }
}
fn phases() -> PhaseBudget {
    PhaseBudget {
        compile: Span::ZERO,
        startup: span(B::WorkerStartup),
        service: span(B::UnknownMemberWall),
        cleanup: span(B::WorkerCleanupReserve),
    }
}
fn restart() -> RestartBudget {
    RestartBudget {
        stop: span(B::ObservedStop),
        start: span(B::WorkerStartup),
        safety: span(B::WorkerCleanupReserve),
    }
}
fn caps(owner: bool, storage: u64) -> node::Caps {
    node::Caps {
        storage_bytes: storage,
        memory_bytes: 4 * 1024 * 1024 * 1024,
        wall_ms: span(if owner {
            B::WorkerDefaultWall
        } else {
            B::MemberChosenWall
        })
        .millis(),
        commands: B::PyFinalMyceliumA13MacosModelResourcesMaxCommandsL562C82
            .count()
            .unwrap()
            .get(),
    }
}
fn binding() -> node::Binding {
    node::Binding {
        swarm_id: "swarm".into(),
        node_id: "b-owner".into(),
        seed_key_digest: "fixture-owner-key".into(),
    }
}
fn setup(t: &Temp, c: &Value, storage: u64) -> (node::Core, Vec<AdmittedMember>) {
    setup_generation(t, c, storage, 2)
}
fn setup_generation(
    t: &Temp,
    c: &Value,
    storage: u64,
    generation: u64,
) -> (node::Core, Vec<AdmittedMember>) {
    setup_generation_at(t, c, storage, generation, 0)
}
fn setup_generation_at(
    t: &Temp,
    c: &Value,
    storage: u64,
    generation: u64,
    at: u64,
) -> (node::Core, Vec<AdmittedMember>) {
    let key = Identity::from_secret("endpoint-member", &[9; 32]).unwrap();
    let id = MemberIdentity {
        node_id: "a-member".into(),
        generation,
        incarnation: "boot".into(),
        endpoint_id: key.endpoint_id().into(),
        verification_key_digest: key.verification_key_digest(),
    };
    let (mut core, _) = node::Core::open_with_policy(
        &t.db(),
        binding(),
        caps(true, 2_000_000_000),
        now(at),
        span(B::PyMyceliumA13MacosManagedControlManagedcontrolmailboxConnectTimeoutL823),
    )
    .unwrap();
    core.join(&control::MemberBinding {
        target_node_id: id.node_id.clone(),
        target_endpoint_id: id.endpoint_id.clone(),
        target_key_digest: id.verification_key_digest.clone(),
        membership_generation: generation.to_string(),
        member_incarnation: id.incarnation.clone(),
        run_id: "run".into(),
        deployment_id: "deploy".into(),
    })
    .unwrap();
    let bounds = node::ConsentBounds {
        minimum_storage: Bytes::ZERO,
        policy_storage: B::FreshStorageConsent.bytes().unwrap(),
        minimum_memory: Bytes::ZERO,
        maximum_memory: Bytes::new(4 * 1024 * 1024 * 1024),
        device_memory_ceiling: Bytes::new(4 * 1024 * 1024 * 1024),
        minimum_wall: Span::ZERO,
        maximum_wall: span(B::WorkerMaximumWall),
        maximum_commands: Entries::new(caps(false, storage).commands),
    };
    core.consent(
        node::Consent::record(
            true,
            &node::disclosure_digest(),
            caps(false, storage),
            node::Binding {
                node_id: id.node_id.clone(),
                ..binding()
            },
            &bounds,
            0,
        )
        .unwrap(),
    )
    .unwrap();
    core.report(
        &id.node_id,
        &generation.to_string(),
        core.state().epoch,
        WallExpiry::from_unix_millis(at + span(B::MembershipTtl).millis()),
        now(at),
    )
    .unwrap();
    let mut members = Vec::new();
    for row in c["members"].as_array().unwrap() {
        let member: placement::Member = serde_json::from_value(row.clone()).unwrap();
        if member.node_id == "b-owner" {
            let oid = MemberIdentity {
                node_id: member.node_id.clone(),
                generation: 1,
                incarnation: "local".into(),
                endpoint_id: "local".into(),
                verification_key_digest: "local".into(),
            };
            members.push(
                local_owner(
                    oid,
                    member,
                    span(B::WorkerDefaultWall),
                    now(at + span(B::WorkerMaximumWall).millis()).mono,
                    now(at).mono,
                )
                .unwrap(),
            );
        } else {
            let m = json!({"protocol":"mycelium.membership.capability_report.v1","message_id":"report","swarm_id":"swarm","sender_node_id":id.node_id,"sender_endpoint_id":id.endpoint_id,"recipient_node_id":"b-owner","incarnation":id.incarnation,"generation":generation,"issued_at":at/1000,"expires_at":(at+span(B::MemberChosenWall).millis())/1000,"platform":"macos","architecture":"arm64","memory_bytes":member.memory_bytes,"available_storage_bytes":member.available_storage_bytes.unwrap_or(8_000_000_000),"backends":["mlx"],"precisions":["float32"]});
            let raw=serde_json::to_vec(&json!({"protocol":protocol::membership::SIGNED_MESSAGE_PROTOCOL,"message":m,"signature":key.sign(&m).unwrap(),"verification_key":key.public_key_record()})).unwrap();
            // Report caps deliberately retain the original 2GB: adapter must intersect CURRENT L6 consent.
            let verifier = MembershipVerifier {
                swarm_id: "swarm",
                recipient_node_id: "b-owner",
                caps: member.caps.unwrap(),
                backend: "mlx",
                precision: "float32",
                maximum_bytes: raw.len(),
            };
            members.push(
                joined_member(
                    &verifier,
                    &raw,
                    &id,
                    span(B::WorkerDefaultWall),
                    now(at).wall,
                    now(at).mono,
                )
                .unwrap(),
            );
        }
    }
    (core, members)
}
fn tickets(p: &Prepared) -> Vec<node::Ticket> {
    p.starts()
        .iter()
        .map(|e| match e {
            node::Effect::Start { ticket, .. } => ticket.clone(),
            _ => panic!("not start"),
        })
        .collect()
}
struct Environment(node::Residue);
impl node::WorkerAdapter for Environment {
    fn request_stop(&mut self) -> Result<(), node::WorkerError> {
        Ok(())
    }
    fn observe(&mut self) -> Result<node::Residue, node::WorkerError> {
        Ok(self.0)
    }
}
fn observed(ticket: &node::Ticket, resources_gone: bool) -> node::CleanupObservation {
    node::Supervisor::new(
        ticket.clone(),
        Environment(node::Residue {
            process_gone: true,
            resources_gone,
        }),
    )
    .observe_cleanup()
    .unwrap()
}
fn pending_rollover(core: &mut node::Core, prepared: &Prepared) -> u64 {
    for ticket in tickets(prepared) {
        core.started(&ticket, now(0).mono, now(0)).unwrap();
    }
    let at = core.state().rollover_at.unwrap().ticks;
    assert_eq!(at, 480_000); // v0.1.4: 600s member minus 120s lead.
    let mut renewal = span(B::LeaseRenewalInterval).millis();
    while renewal < at {
        core.report(
            "a-member",
            "2",
            core.state().epoch,
            WallExpiry::from_unix_millis(renewal + span(B::MembershipTtl).millis()),
            now(renewal),
        )
        .unwrap();
        renewal += span(B::LeaseRenewalInterval).millis();
    }
    assert!(
        core.tick(now(at - 1))
            .unwrap()
            .iter()
            .all(|e| !matches!(e, node::Effect::Stop { .. }))
    );
    assert_eq!(
        core.tick(now(at))
            .unwrap()
            .iter()
            .filter(|e| matches!(e, node::Effect::Stop { .. }))
            .count(),
        2
    );
    assert!(core.state().rollover_members.is_some());
    at
}
fn rollover_reports(members: &[AdmittedMember], at: u64) -> Vec<node::RolloverReport> {
    members
        .iter()
        .filter(|m| m.source() != budgets::AuthoritySource::LocalProposal)
        .map(|m| {
            let id = m.identity();
            node::RolloverReport {
                binding: control::MemberBinding {
                    target_node_id: id.node_id.clone(),
                    target_endpoint_id: id.endpoint_id.clone(),
                    target_key_digest: id.verification_key_digest.clone(),
                    membership_generation: id.generation.to_string(),
                    member_incarnation: id.incarnation.clone(),
                    run_id: "run".into(),
                    deployment_id: "deploy".into(),
                },
                expiry: WallExpiry::from_unix_millis(at + span(B::MembershipTtl).millis()),
            }
        })
        .collect()
}
// O:incident:F15: real L6 tick/continuation, real L7 placement, injected clocks.
#[test]
fn timed_rollover_continuation_plans_successor_after_cleanup_with_fresh_signed_caps() {
    let t = Temp::new();
    let fresh = Temp::new();
    let (f, c) = fixture();
    let (mut core, members) = setup(&t, &c, 2_000_000_000);
    let first = prepare(
        &mut core,
        input(&f, &c),
        &members,
        now(0),
        phases(),
        restart(),
    )
    .unwrap();
    let at = pending_rollover(&mut core, &first);
    let (_, members) = setup_generation_at(&fresh, &c, 2_000_000_000, 2, at);
    let reports = rollover_reports(&members, at);
    assert!(matches!(
        continue_rollover(
            &mut core,
            input(&f, &c),
            &members,
            &reports,
            now(at),
            phases(),
            &restart()
        ),
        Err(Error::Control(node::Error::Refused(
            "rollover_cleanup_unproven"
        )))
    ));
    for ticket in tickets(&first) {
        core.cleaned(&observed(&ticket, true), now(at)).unwrap();
    }
    let next = continue_rollover(
        &mut core,
        input(&f, &c),
        &members,
        &reports,
        now(at),
        phases(),
        &restart(),
    )
    .unwrap();
    assert_eq!(next.placement(), &c["expected"]);
    assert!(core.state().rollover_members.is_none());
    assert_eq!(core.state().run, 2);
    for ticket in tickets(&next) {
        assert_eq!(ticket.run, 2);
        core.started(&ticket, now(at).mono, now(at)).unwrap();
    }
    assert_eq!(
        core.state().workers["a-member"].expires.unwrap().ticks,
        at + span(B::MemberChosenWall).millis()
    );
    assert_eq!(
        core.state().workers["b-owner"].expires.unwrap().ticks,
        at + span(B::WorkerDefaultWall).millis()
    );
    assert!(matches!(
        continue_rollover(
            &mut core,
            input(&f, &c),
            &members,
            &reports,
            now(at),
            phases(),
            &restart()
        ),
        Err(Error::Control(node::Error::Refused("rollover_not_pending")))
    ));
}
#[test]
fn timed_rollover_continuation_member_loss_is_consumed_without_retry() {
    let (f, c) = fixture();
    for fault in ["missing", "changed", "duplicate"] {
        let t = Temp::new();
        let fresh = Temp::new();
        let (mut core, members) = setup(&t, &c, 2_000_000_000);
        let first = prepare(
            &mut core,
            input(&f, &c),
            &members,
            now(0),
            phases(),
            restart(),
        )
        .unwrap();
        let at = pending_rollover(&mut core, &first);
        for ticket in tickets(&first) {
            core.cleaned(&observed(&ticket, true), now(at)).unwrap();
        }
        let (_, members) = setup_generation_at(&fresh, &c, 2_000_000_000, 2, at);
        let mut reports = rollover_reports(&members, at);
        match fault {
            "missing" => reports.clear(),
            "changed" => reports[0].binding.member_incarnation = "changed".into(),
            _ => reports.extend(rollover_reports(&members, at)),
        }
        // A lost collection may lose both descriptors and capabilities; L6's terminal
        // membership diagnostic must win over the L7 input-count refusal.
        let capabilities = if fault == "missing" {
            &[][..]
        } else {
            &members[..]
        };
        assert!(matches!(
            continue_rollover(
                &mut core,
                input(&f, &c),
                capabilities,
                &reports,
                now(at),
                phases(),
                &restart()
            ),
            Err(Error::Control(node::Error::Refused(
                "rollover_members_changed"
            )))
        ));
        assert!(core.state().rollover_members.is_none());
        assert_eq!(
            core.state().last_failure.as_ref().unwrap().code,
            "rollover_members_changed"
        );
        assert!(core.state().workers.is_empty());
        assert_eq!(core.state().run, 1);
        assert!(matches!(
            continue_rollover(
                &mut core,
                input(&f, &c),
                &members,
                &rollover_reports(&members, at),
                now(at),
                phases(),
                &restart()
            ),
            Err(Error::Control(node::Error::Refused("rollover_not_pending")))
        ));
    }
}

#[test]
fn timed_rollover_continuation_validates_capability_binding_before_reserving() {
    let t = Temp::new();
    let fresh = Temp::new();
    let (f, c) = fixture();
    let (mut core, members) = setup(&t, &c, 2_000_000_000);
    let first = prepare(
        &mut core,
        input(&f, &c),
        &members,
        now(0),
        phases(),
        restart(),
    )
    .unwrap();
    let at = pending_rollover(&mut core, &first);
    let reports = rollover_reports(&members, at);
    for ticket in tickets(&first) {
        core.cleaned(&observed(&ticket, true), now(at)).unwrap();
    }
    let (_, stale) = setup_generation_at(&fresh, &c, 2_000_000_000, 3, at);
    let before = core.state().clone();
    assert!(matches!(
        continue_rollover(
            &mut core,
            input(&f, &c),
            &stale,
            &reports,
            now(at),
            phases(),
            &restart()
        ),
        Err(Error::Planner(planner::Error {
            code: "member_capability_binding",
            ..
        }))
    ));
    assert_eq!(core.state(), &before);
    assert!(core.state().workers.is_empty());
}

// O:incident:F15; source-pinned member-change refusal, no retry driver.
#[test]
fn rollover_replan_preserves_member_gate_and_durable_cleanup() {
    let t = Temp::new();
    let changed = Temp::new();
    let (f, c) = fixture();
    let (mut core, mut members) = setup(&t, &c, 2_000_000_000);
    let prepared = prepare(
        &mut core,
        input(&f, &c),
        &members,
        now(0),
        phases(),
        restart(),
    )
    .unwrap();
    let (mut other, changed_members) = setup_generation(&changed, &c, 2_000_000_000, 3);
    let before = other.state().clone();
    match prepared
        .replan(
            &mut other,
            input(&f, &c),
            &changed_members,
            now(0),
            phases(),
            restart(),
        )
        .unwrap_err()
    {
        Error::Planner(e) => assert_eq!(e.code, "rollover_members_changed"),
        e => panic!("wrong refusal: {e:?}"),
    }
    assert_eq!(&before, other.state());
    assert!(
        prepared
            .replan(
                &mut core,
                input(&f, &c),
                &members,
                now(0),
                phases(),
                restart()
            )
            .is_err()
    );
    let old = tickets(&prepared);
    core.cancel(now(0)).unwrap();
    assert!(core.cleaned(&observed(&old[0], false), now(0)).is_err());
    assert!(
        prepared
            .replan(
                &mut core,
                input(&f, &c),
                &members,
                now(0),
                phases(),
                restart()
            )
            .is_err()
    );
    for ticket in &old {
        core.cleaned(&observed(ticket, true), now(0)).unwrap();
    }
    core.report(
        "a-member",
        "2",
        core.state().epoch,
        WallExpiry::from_unix_millis(span(B::MembershipTtl).millis()),
        now(0),
    )
    .unwrap();
    members.reverse();
    prepared.check_rollover_members(&members).unwrap();
    members.reverse(); // Placement still uses the caller's original request order.
    let next = prepared
        .replan(
            &mut core,
            input(&f, &c),
            &members,
            now(0),
            phases(),
            restart(),
        )
        .unwrap();
    assert_eq!(next.starts().len(), old.len());
    assert_ne!(tickets(&next), old);
    assert!(core.cleaned(&observed(&old[0], true), now(0)).is_err());
}

// O:incident:F14 O:incident:F15 O:incident:F15b
#[test]
fn actual_signed_report_to_placement_to_sqlite_reservation_and_worker_deadlines() {
    let t = Temp::new();
    let (f, c) = fixture();
    let (mut core, members) = setup(&t, &c, 2_000_000_000);
    let prepared = prepare(
        &mut core,
        input(&f, &c),
        &members,
        now(0),
        phases(),
        restart(),
    )
    .unwrap();
    assert_eq!(prepared.placement(), &c["expected"]);
    assert_eq!(core.state().workers.len(), 2);
    assert!(!core.state().members.contains_key("b-owner"));
    for ticket in tickets(&prepared) {
        let origin = if ticket.node == "b-owner" {
            now(0)
        } else {
            now(10_000)
        };
        core.started(&ticket, origin.mono, now(10_000)).unwrap();
    }
    assert_eq!(
        core.state().workers["b-owner"].expires.unwrap().ticks,
        1_800_000
    );
    assert_eq!(
        core.state().workers["a-member"].expires.unwrap().ticks,
        610_000
    );
    assert_eq!(core.state().rollover_at.unwrap().ticks, 490_000);
    assert!(
        prepare(
            &mut core,
            input(&f, &c),
            &members,
            now(10_000),
            phases(),
            restart()
        )
        .is_err()
    );
    let (reopened, _) = node::Core::open_with_policy(
        &t.db(),
        binding(),
        caps(true, 2_000_000_000),
        now(10_000),
        span(B::PyMyceliumA13MacosManagedControlManagedcontrolmailboxConnectTimeoutL823),
    )
    .unwrap();
    assert_eq!(reopened.state().workers.len(), 2);
    assert_eq!(reopened.state().phase, node::RunPhase::CleanupUnproven);
}
// O:incident:F14 refusal before start/staging; request JSON cannot enlarge consent.
#[test]
fn current_consent_and_physical_staging_refuse_without_durable_effects() {
    let (f, c) = fixture();
    for mode in ["consent", "physical", "time"] {
        let t = Temp::new();
        let mut c = c.clone();
        if mode == "physical" {
            c["members"][0]["available_storage_bytes"] = json!(1);
        }
        let (mut core, members) = setup(&t, &c, if mode == "consent" { 1 } else { 2_000_000_000 });
        let before = core.state().clone();
        let mut phases = phases();
        if mode == "time" {
            phases.compile = span(B::MemberChosenWall);
        }
        let err = prepare(
            &mut core,
            input(&f, &c),
            &members,
            now(0),
            phases,
            restart(),
        )
        .unwrap_err();
        let expected = match mode {
            "consent" => "member_storage_cap_below_share",
            "physical" => "member_disk_insufficient_for_share",
            _ => "member_time_insufficient_for_share",
        };
        assert!(format!("{err:?}").contains(expected), "{mode}: {err:?}");
        assert_eq!(core.state(), &before);
        assert!(core.state().workers.is_empty());
    }
}
// O:incident:F16 cleanup is L6's environmental observation, never a planner bool.
#[test]
fn unproven_cleanup_retains_durable_capacity_and_old_observation_cannot_settle_successor() {
    let t = Temp::new();
    let (f, c) = fixture();
    let (mut core, members) = setup(&t, &c, 2_000_000_000);
    let prepared = prepare(
        &mut core,
        input(&f, &c),
        &members,
        now(0),
        phases(),
        restart(),
    )
    .unwrap();
    let old = tickets(&prepared);
    core.cancel(now(0)).unwrap();
    assert!(core.cleaned(&observed(&old[0], false), now(0)).is_err());
    assert_eq!(core.state().workers.len(), 2);
    assert!(
        prepare(
            &mut core,
            input(&f, &c),
            &members,
            now(0),
            phases(),
            restart()
        )
        .is_err()
    );
    for ticket in &old {
        core.cleaned(&observed(ticket, true), now(0)).unwrap();
    }
    core.report(
        "a-member",
        "2",
        core.state().epoch,
        WallExpiry::from_unix_millis(span(B::MembershipTtl).millis()),
        now(0),
    )
    .unwrap();
    prepare(
        &mut core,
        input(&f, &c),
        &members,
        now(0),
        phases(),
        restart(),
    )
    .unwrap();
    assert!(core.cleaned(&observed(&old[0], true), now(0)).is_err());
    assert_eq!(core.state().workers.len(), 2);
}
#[test]
fn compile_failure_keeps_both_primary_and_durable_stop_errors() {
    let t = Temp::new();
    let (f, c) = fixture();
    let (mut core, members) = setup(&t, &c, 2_000_000_000);
    let mut p = phases();
    p.compile = span(B::WorkerStartup);
    let mut prepared = prepare(&mut core, input(&f, &c), &members, now(0), p, restart()).unwrap();
    let out = prepared.compile(&mut core, &mut || now(0), |_| {
        // A real reopen fences the old writer while compiler work is in flight.
        let (reopened, _) = node::Core::open_with_policy(
            &t.db(),
            binding(),
            caps(true, 2_000_000_000),
            now(0),
            span(B::PyMyceliumA13MacosManagedControlManagedcontrolmailboxConnectTimeoutL823),
        )
        .unwrap();
        assert_eq!(reopened.state().workers.len(), 2);
        Err(planner::Error::new("injected_compile_failure"))
    });
    match out.unwrap_err() {
        planner::staged::CompileFailure::Failed { cause, stop } => {
            assert_eq!(cause.code, "injected_compile_failure");
            assert!(stop.is_err(), "old owner must not publish successful stop");
        }
        other => panic!("unexpected: {other:?}"),
    }
    assert_eq!(core.state().workers.len(), 2);
}
#[test]
fn signed_report_from_previous_membership_refuses_before_plan_mutation() {
    let old = Temp::new();
    let current = Temp::new();
    let (f, c) = fixture();
    let (_old_core, members) = setup(&old, &c, 2_000_000_000);
    let (mut core, _current_members) = setup_generation(&current, &c, 2_000_000_000, 3);
    let before = core.state().clone();
    assert!(
        format!(
            "{:?}",
            prepare(
                &mut core,
                input(&f, &c),
                &members,
                now(0),
                phases(),
                restart()
            )
            .unwrap_err()
        )
        .contains("member_capability_binding")
    );
    assert_eq!(core.state(), &before);
}
// O:incident:F16 O:incident:F15: faithful injected phase failure, no model loaded.
#[test]
fn compiler_failure_and_late_success_are_explicit_and_keep_reservations() {
    let (f, c) = fixture();
    for fault in ["compiler", "late", "close", "positive"] {
        let t = Temp::new();
        let (mut core, members) = setup(&t, &c, 2_000_000_000);
        let mut p = phases();
        p.compile = span(B::WorkerStartup);
        let mut prepared =
            prepare(&mut core, input(&f, &c), &members, now(0), p, restart()).unwrap();
        let mut calls = 0;
        let mut samples = 0;
        let mut clock = || {
            samples += 1;
            now(if samples > 1 && fault == "late" {
                p.compile.millis()
            } else {
                0
            })
        };
        let out = prepared.compile(&mut core, &mut clock, |window| {
            calls += 1;
            assert_eq!(window.deadline().ticks(), p.compile.millis());
            if fault == "compiler" {
                Err(planner::Error::new("injected_compile_failure"))
            } else {
                Ok(json!({"compiled":true}))
            }
        });
        assert_eq!(calls, 1, "{fault}");
        if fault == "positive" || fault == "close" {
            assert_eq!(out.unwrap(), json!({"compiled":true}));
        } else {
            match out.unwrap_err() {
                planner::staged::CompileFailure::Failed { cause, stop } => {
                    assert_eq!(
                        cause.code,
                        if fault == "late" {
                            "compile_deadline_expired"
                        } else {
                            "injected_compile_failure"
                        }
                    );
                    assert_eq!(stop.unwrap().len(), 2);
                }
                other => panic!("wrong failure: {other:?}"),
            }
            assert_eq!(core.state().phase, node::RunPhase::Stopping);
        }
        assert_eq!(core.state().workers.len(), 2);
        let mut second_calls = 0;
        assert!(
            prepared
                .compile(&mut core, &mut || now(0), |_| {
                    second_calls += 1;
                    Ok(json!({}))
                })
                .is_err()
        );
        assert_eq!(second_calls, 0);
        if fault == "close" {
            core.cancel(now(0)).unwrap();
            let ticket = tickets(&prepared)[0].clone();
            struct FailedClose;
            impl node::WorkerAdapter for FailedClose {
                fn request_stop(&mut self) -> Result<(), node::WorkerError> {
                    Err(node::WorkerError::Refused("injected_close_failure"))
                }
                fn observe(&mut self) -> Result<node::Residue, node::WorkerError> {
                    Err(node::WorkerError::Refused("injected_observe_failure"))
                }
            }
            let mut supervisor = node::Supervisor::new(ticket, FailedClose);
            assert!(supervisor.request_stop().is_err());
            assert!(supervisor.observe_cleanup().is_err());
            assert_eq!(core.state().workers.len(), 2);
        }
    }
}
#[test]
fn expired_compile_and_foreign_successor_refuse_before_callback() {
    let t = Temp::new();
    let (f, c) = fixture();
    let (mut core, members) = setup(&t, &c, 2_000_000_000);
    let mut p = phases();
    p.compile = span(B::WorkerStartup);
    let mut prepared = prepare(&mut core, input(&f, &c), &members, now(0), p, restart()).unwrap();
    let mut calls = 0;
    assert!(
        prepared
            .compile(&mut core, &mut || now(p.compile.millis()), |_| {
                calls += 1;
                Ok(json!({}))
            })
            .is_err()
    );
    assert_eq!(calls, 0);
    assert_eq!(core.state().phase, node::RunPhase::Stopping);
    for ticket in tickets(&prepared) {
        core.cleaned(&observed(&ticket, true), now(p.compile.millis()))
            .unwrap();
    }
    core.report(
        "a-member",
        "2",
        core.state().epoch,
        WallExpiry::from_unix_millis(span(B::MembershipTtl).millis()),
        now(p.compile.millis()),
    )
    .unwrap();
    let next = prepare(
        &mut core,
        input(&f, &c),
        &members,
        now(p.compile.millis()),
        p,
        restart(),
    )
    .unwrap();
    let before = core.state().clone();
    assert!(
        prepared
            .compile(&mut core, &mut || now(p.compile.millis()), |_| {
                calls += 1;
                Ok(json!({}))
            })
            .is_err()
    );
    assert_eq!(calls, 0);
    assert_eq!(core.state(), &before);
    assert_eq!(tickets(&next).len(), 2);
}
