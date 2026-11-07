// SPDX-License-Identifier: AGPL-3.0-only
//! Real L5/L6/L7 composition and owned OS children; clocks/capacity are fixtures.
//! This is not Metal/Iroh continuation or persistent member-host qualification.
use backend_mlx::{
    process::{OwnedWorker, ProcessLimits},
    resources::ResourcePool,
};
use budgets::{
    BudgetId as B, Bytes, ClockDomain, Entries, MonoInstant, Reservation, Span, WallExpiry,
};
use node_service::{
    enrollment::{self, Invitation, LocalMembership},
    planning,
    worker::LeafAdapter,
};
use planner::{
    admission::{PhaseBudget, PlanInputs},
    discovery::{AdmittedMember, MemberIdentity, MembershipVerifier, joined_member, local_owner},
};
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    process::Command,
    time::{Duration, Instant},
};
fn span(id: B) -> Span {
    id.span(Entries::ZERO).unwrap()
}
fn now(at: u64) -> node::Now {
    node::Now {
        mono: MonoInstant::new(ClockDomain(1), at),
        wall: WallExpiry::from_unix_millis(at),
    }
}
struct Fixture {
    root: PathBuf,
    data: Value,
    core: node::Core,
    owner: protocol::identity::Identity,
    member: protocol::identity::Identity,
    local: LocalMembership,
    binding: control::MemberBinding,
}
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
            "slice-rollover-{}",
            node_service::service::random_id().unwrap()
        ));
        let owner = enrollment::identity(&root.join("owner")).unwrap();
        let member = enrollment::identity(&root.join("member")).unwrap();
        let data: Value =
            serde_json::from_str(include_str!("../data/qwen2-planning.json")).unwrap();
        let caps = node::Caps {
            storage_bytes: B::SavedStorageConsent.bytes().unwrap().get(),
            memory_bytes: B::FreshStorageConsent.bytes().unwrap().get(),
            wall_ms: span(B::MemberChosenWall).millis(),
            commands: 1,
        };
        let invite = Invitation {
            swarm: "rollover-fixture".into(),
            owner_node: "a-owner".into(),
            owner_endpoint: owner.endpoint_id().into(),
            owner_key: owner.public_key_record(),
            member_node: "b-member".into(),
            member_key: member.public_key_record(),
            nonce: "fixture-invite".into(),
        };
        let local = LocalMembership::approve(
            &root.join("member"),
            invite,
            caps.clone(),
            true,
            now(0).wall,
        )
        .unwrap();
        let join = enrollment::join(
            &member,
            &local.invitation,
            "persistent-member-host",
            "127.0.0.1:12345",
            now(0).wall,
        )
        .unwrap();
        let acceptance =
            enrollment::accept(&owner, &local.invitation, &join, 1, now(0).wall).unwrap();
        local
            .verify_acceptance(&join, &acceptance, now(0).wall)
            .unwrap();
        let binding = control::MemberBinding {
            target_node_id: "b-member".into(),
            target_endpoint_id: member.endpoint_id().into(),
            target_key_digest: member.verification_key_digest(),
            membership_generation: "1".into(),
            member_incarnation: "persistent-member-host".into(),
            run_id: "fixture".into(),
            deployment_id: "fixture".into(),
        };
        let core = node_service::lifecycle::open(
            &root.join("node.sqlite"),
            node_service::lifecycle::Admission {
                owner: node::Binding {
                    swarm_id: local.invitation.swarm.clone(),
                    node_id: "a-owner".into(),
                    seed_key_digest: owner.verification_key_digest(),
                },
                policy: node::Caps {
                    storage_bytes: B::FreshStorageConsent.bytes().unwrap().get(),
                    ..caps
                },
                member: binding.clone(),
                owner_key: owner.public_key_record(),
                acceptance,
                consent: Some(local.consent.clone()),
            },
            now(0),
        )
        .unwrap();
        Self {
            root,
            data,
            core,
            owner,
            member,
            local,
            binding,
        }
    }
    fn members(&self, at: u64) -> Vec<AdmittedMember> {
        let caps = &self.local.consent.caps;
        let row = |node: &str| planner::placement::Member {
            node_id: node.into(),
            memory_bytes: caps.memory_bytes,
            available_storage_bytes: Some(B::FreshStorageConsent.bytes().unwrap().get()),
            decode_tokens_per_second: None,
            caps: Some(planner::placement::Caps {
                storage_bytes: Some(if node == "a-owner" {
                    self.core.state().owner_policy.storage_bytes
                } else {
                    caps.storage_bytes
                }),
                max_memory_bytes: Some(caps.memory_bytes),
                max_wall_seconds: Some(caps.wall_ms / 1000),
            }),
        };
        let owner_id = MemberIdentity {
            node_id: "a-owner".into(),
            generation: 1,
            incarnation: "local-owner".into(),
            endpoint_id: self.owner.endpoint_id().into(),
            verification_key_digest: self.owner.verification_key_digest(),
        };
        let owner = local_owner(
            owner_id,
            row("a-owner"),
            span(B::MemberChosenWall),
            now(at).mono.checked_add(span(B::MemberChosenWall)).unwrap(),
            now(at).mono,
        )
        .unwrap();
        let id = MemberIdentity {
            node_id: self.binding.target_node_id.clone(),
            generation: 1,
            incarnation: self.binding.member_incarnation.clone(),
            endpoint_id: self.binding.target_endpoint_id.clone(),
            verification_key_digest: self.binding.target_key_digest.clone(),
        };
        let message = json!({"protocol":"mycelium.membership.capability_report.v1","swarm_id":self.local.invitation.swarm,"message_id":format!("report-{at}"),"sender_node_id":id.node_id,"sender_endpoint_id":id.endpoint_id,"recipient_node_id":"a-owner","platform":"darwin","architecture":"arm64","issued_at":at as f64/1000.,"expires_at":(at+span(B::MembershipTtl).millis()) as f64/1000.,"generation":1,"incarnation":id.incarnation,"memory_bytes":caps.memory_bytes,"available_storage_bytes":B::FreshStorageConsent.bytes().unwrap().get(),"backends":["mlx"],"precisions":["int8-weight-only"]});
        let raw = serde_json::to_vec(&enrollment::signed(&self.member, message).unwrap()).unwrap();
        let member = joined_member(
            &MembershipVerifier {
                swarm_id: &self.local.invitation.swarm,
                recipient_node_id: "a-owner",
                caps: row("b-member").caps.unwrap(),
                backend: "mlx",
                precision: "int8-weight-only",
                maximum_bytes: api::requests::MAX_REQUEST_BODY,
            },
            &raw,
            &id,
            span(B::MemberChosenWall),
            now(at).wall,
            now(at).mono,
        )
        .unwrap();
        vec![owner, member]
    }
    fn report(&self, at: u64) -> node::RolloverReport {
        // Authenticate a fresh owner acceptance before translating its lease.
        let join = enrollment::join(
            &self.member,
            &self.local.invitation,
            &self.binding.member_incarnation,
            "127.0.0.1:12345",
            now(at).wall,
        )
        .unwrap();
        let acceptance =
            enrollment::accept(&self.owner, &self.local.invitation, &join, 1, now(at).wall)
                .unwrap();
        self.local
            .verify_acceptance(&join, &acceptance, now(at).wall)
            .unwrap();
        node::RolloverReport {
            binding: self.binding.clone(),
            expiry: WallExpiry::from_unix_millis(
                (acceptance["message"]["lease_expires_at"].as_f64().unwrap() * 1000.).round()
                    as u64,
            ),
        }
    }
}
fn input<'a>(data: &'a Value, request: &'a Value) -> PlanInputs<'a> {
    PlanInputs {
        model: &data["model"],
        policy: &data["policy"],
        resources: &data["resources"],
        request,
    }
}
fn phases() -> PhaseBudget {
    PhaseBudget {
        compile: span(B::Descriptors),
        startup: span(B::WorkerStartup),
        service: span(B::UnknownMemberWall),
        cleanup: span(B::WorkerCleanupReserve),
    }
}
fn restart() -> planning::RestartBudget {
    planning::RestartBudget {
        stop: span(B::RouterClose),
        start: span(B::WorkerStartup),
        safety: span(B::WorkerCleanupReserve),
    }
}
fn launch(
    core: &mut node::Core,
    prepared: &planning::Prepared,
    at: u64,
) -> Vec<node::Supervisor<LeafAdapter>> {
    prepared
        .starts()
        .iter()
        .map(|effect| {
            let node::Effect::Start { ticket, .. } = effect else {
                panic!("unexpected start effect")
            };
            let amount = Reservation {
                resident: Bytes::new(1),
                staged: Bytes::ZERO,
                staging_overhead: Bytes::ZERO,
                scratch: Bytes::ZERO,
                kv: Bytes::ZERO,
            };
            let pool = ResourcePool::new(amount.total().unwrap());
            let mut command = Command::new("/bin/sleep");
            command.arg((span(B::WorkerDefaultWall).millis() / 1000).to_string());
            let child = OwnedWorker::spawn(
                command,
                pool,
                amount,
                ProcessLimits {
                    deadline: Instant::now()
                        + Duration::from_millis(span(B::WorkerStartup).millis()),
                    close: span(B::RouterClose),
                    reap: span(B::WorkerCleanupReserve),
                    poll: span(B::RegistryPoll),
                },
            )
            .unwrap();
            let mut worker = node::Supervisor::new(ticket.clone(), LeafAdapter::new(child));
            assert!(!worker.observe_cleanup().unwrap().proven());
            core.started(ticket, now(at).mono, now(at)).unwrap();
            worker
        })
        .collect()
}
fn clean(
    core: &mut node::Core,
    workers: &mut [node::Supervisor<LeafAdapter>],
    at: u64,
) -> Vec<node::CleanupObservation> {
    workers
        .iter_mut()
        .map(|worker| {
            worker.request_stop().unwrap();
            let observation = worker.observe_cleanup().unwrap();
            assert!(observation.proven());
            core.cleaned(&observation, now(at)).unwrap();
            observation
        })
        .collect()
}
#[test]
fn wall_tick_continues_same_signed_members_after_real_leaf_cleanup_and_stop_fences_successor() {
    let mut f = Fixture::new();
    let members = f.members(0);
    let request = json!({"owner_node_id":"a-owner","model_id":f.data["model"]["model_id"],"quantization":"int8-weight-only","slice_mode":false,"saved":true,"members":members.iter().map(|m|m.member()).collect::<Vec<_>>()});
    let prepared = planning::prepare(
        &mut f.core,
        input(&f.data, &request),
        &members,
        now(0),
        phases(),
        restart(),
    )
    .unwrap();
    let mut workers = launch(&mut f.core, &prepared, 0);
    let at = f.core.state().rollover_at.unwrap().ticks;
    let mut renewal = span(B::LeaseRenewalInterval).millis();
    while renewal < at {
        let report = f.report(renewal);
        f.core
            .report(
                "b-member",
                "1",
                f.core.state().epoch,
                report.expiry,
                now(renewal),
            )
            .unwrap();
        renewal += span(B::LeaseRenewalInterval).millis();
    }
    assert!(
        f.core
            .tick(now(at - 1))
            .unwrap()
            .iter()
            .all(|e| !matches!(e, node::Effect::Stop { .. }))
    );
    assert_eq!(
        f.core
            .tick(now(at))
            .unwrap()
            .iter()
            .filter(|e| matches!(e, node::Effect::Stop { .. }))
            .count(),
        workers.len()
    );
    let members = f.members(at);
    let reports = [f.report(at)];
    assert!(matches!(
        planning::continue_rollover(
            &mut f.core,
            input(&f.data, &request),
            &members,
            &reports,
            now(at),
            phases(),
            &restart()
        ),
        Err(planning::Error::Control(node::Error::Refused(
            "rollover_cleanup_unproven"
        )))
    ));
    let old = clean(&mut f.core, &mut workers, at);
    let successor = planning::continue_rollover(
        &mut f.core,
        input(&f.data, &request),
        &members,
        &reports,
        now(at),
        phases(),
        &restart(),
    )
    .unwrap();
    assert_eq!(f.core.state().run, 2);
    assert!(f.core.state().rollover_members.is_none());
    let mut workers = launch(&mut f.core, &successor, at);
    assert_eq!(f.core.state().phase, node::RunPhase::Serving);
    for observation in &old {
        assert!(f.core.cleaned(observation, now(at)).is_err());
    }
    let stop = f.core.request_stop(now(at)).unwrap();
    assert!(f.core.finish_stop(&stop.fence).is_err());
    clean(&mut f.core, &mut workers, at);
    let proof = f.core.finish_stop(&stop.fence).unwrap();
    assert_eq!(proof.run(), 2);
    assert!(matches!(
        planning::continue_rollover(
            &mut f.core,
            input(&f.data, &request),
            &members,
            &reports,
            now(at),
            phases(),
            &restart()
        ),
        Err(planning::Error::Control(node::Error::Refused(
            "rollover_not_pending"
        )))
    ));
    drop(f.core);
    std::fs::remove_dir_all(f.root).unwrap();
}
