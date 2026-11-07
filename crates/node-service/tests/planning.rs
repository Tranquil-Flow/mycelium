// SPDX-License-Identifier: AGPL-3.0-only
//! Controlled capability verifier only here; no signed membership claim.
use budgets::{BudgetId, ClockDomain, Entries, MonoInstant, Span, WallExpiry};

use planner::discovery::*;
use serde_json::Value;
fn span(id: BudgetId) -> Span {
    id.span(Entries::ZERO).unwrap()
}
#[test]
fn l6_planner_adapter_reserves_real_l7_placement_and_refuses_reentry() {
    let f: Value =
        serde_json::from_str(include_str!("../../planner/tests/fixtures/planning.json")).unwrap();
    let case = f["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["legacy_saved_selection"] == true && c.get("expected").is_some())
        .unwrap();
    let now = MonoInstant::new(ClockDomain(1), 0);
    let mut admitted = vec![];
    let root = std::env::temp_dir().join(format!(
        "slice-planning-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::create_dir(&root).unwrap();
    let caps = node::Caps {
        storage_bytes: 2_000_000_000,
        memory_bytes: 4_294_967_296,
        wall_ms: span(BudgetId::WorkerDefaultWall).millis(),
        commands: 1,
    };
    let binding = node::Binding {
        swarm_id: "swarm".into(),
        node_id: case["owner_node_id"].as_str().unwrap().into(),
        seed_key_digest: "key".into(),
    };
    let current = node::Now {
        mono: now,
        wall: WallExpiry::from_unix_millis(0),
    };
    let (mut core, _) = node::Core::open_with_policy(
        &root.join("node.sqlite"),
        binding.clone(),
        caps.clone(),
        current,
        span(BudgetId::PyBoundaryPhysicalSqliteCapacityL72C15),
    )
    .unwrap();
    struct Verified(VerifiedReport);
    impl CapabilityVerifier for Verified {
        fn verify(
            &self,
            _: &[u8],
            _: &MemberIdentity,
            _: WallExpiry,
        ) -> planner::Result<VerifiedReport> {
            Ok(self.0.clone())
        }
    }
    for row in case["members"].as_array().unwrap() {
        let m: planner::placement::Member = serde_json::from_value(row.clone()).unwrap();
        let id = MemberIdentity {
            node_id: m.node_id.clone(),
            generation: 1,
            incarnation: "boot".into(),
            endpoint_id: format!("ep-{}", m.node_id),
            verification_key_digest: format!("sha256:{}", "a".repeat(64)),
        };
        let owner = case["owner_node_id"] == m.node_id;
        if !owner {
            core.join(&control::MemberBinding {
                target_node_id: id.node_id.clone(),
                target_endpoint_id: id.endpoint_id.clone(),
                target_key_digest: id.verification_key_digest.clone(),
                membership_generation: "1".into(),
                member_incarnation: id.incarnation.clone(),
                run_id: "run".into(),
                deployment_id: "deployment".into(),
            })
            .unwrap();
            core.consent(
                node::Consent::record(
                    true,
                    &node::disclosure_digest(),
                    caps.clone(),
                    node::Binding {
                        node_id: id.node_id.clone(),
                        ..binding.clone()
                    },
                    &node::ConsentBounds {
                        minimum_storage: budgets::Bytes::ZERO,
                        policy_storage: budgets::Bytes::new(caps.storage_bytes),
                        minimum_memory: budgets::Bytes::ZERO,
                        maximum_memory: budgets::Bytes::new(caps.memory_bytes),
                        device_memory_ceiling: budgets::Bytes::new(caps.memory_bytes),
                        minimum_wall: Span::ZERO,
                        maximum_wall: span(BudgetId::WorkerMaximumWall),
                        maximum_commands: Entries::new(caps.commands),
                    },
                    0,
                )
                .unwrap(),
            )
            .unwrap();
            core.report(
                &id.node_id,
                "1",
                core.state().epoch,
                WallExpiry::from_unix_millis(span(BudgetId::MembershipTtl).millis()),
                current,
            )
            .unwrap();
        }
        let wall = span(BudgetId::WorkerDefaultWall);
        admitted.push(if owner {
            local_owner(id, m, wall, now.checked_add(wall).unwrap(), now).unwrap()
        } else {
            joined_member(
                &Verified(VerifiedReport {
                    identity: id.clone(),
                    member: m,
                    expires_at: WallExpiry::from_unix_millis(wall.millis()),
                }),
                b"fixture",
                &id,
                wall,
                WallExpiry::from_unix_millis(0),
                now,
            )
            .unwrap()
        });
    }
    let model = f["models"]
        .as_array()
        .unwrap()
        .iter()
        .find(|m| m["model_id"] == case["model_id"])
        .unwrap()
        .clone();
    let phases = planner::admission::PhaseBudget {
        compile: Span::ZERO,
        startup: Span::ZERO,
        service: span(BudgetId::UnknownMemberWall),
        cleanup: span(BudgetId::WorkerCleanupReserve),
    };
    let restart = node_service::planning::RestartBudget {
        stop: span(BudgetId::ObservedStop),
        start: span(BudgetId::WorkerStartup),
        safety: span(BudgetId::WorkerCleanupReserve),
    };
    let input = || planner::admission::PlanInputs {
        model: &model,
        policy: &f["policies"]["int8-weight-only"],
        resources: &f["resources"],
        request: case,
    };
    let p =
        node_service::planning::prepare(&mut core, input(), &admitted, current, phases, restart)
            .unwrap();
    let plan = core.state().plan.as_ref().unwrap();
    assert_eq!(plan.assignments.len(), 2);
    assert_eq!(p.placement(), &case["expected"]);
    assert_eq!(core.state().phase, node::RunPhase::Starting);
    assert_eq!(core.state().workers.len(), 2);
    assert!(
        node_service::planning::prepare(&mut core, input(), &admitted, current, phases, restart)
            .is_err()
    );
    core.cancel(current).unwrap();
    // Empty cleanup cannot release durable reservations or admit a successor.
    assert_eq!(core.state().workers.len(), 2);
    assert!(
        node_service::planning::prepare(&mut core, input(), &admitted, current, phases, restart)
            .is_err()
    );
    drop(core);
    let (core, recovery) = node::Core::open_with_policy(
        &root.join("node.sqlite"),
        binding,
        caps,
        current,
        span(BudgetId::PyBoundaryPhysicalSqliteCapacityL72C15),
    )
    .unwrap();
    assert_eq!(core.state().workers.len(), 2);
    assert!(!recovery.is_empty());
    drop(core);
    std::fs::remove_dir_all(root).unwrap();
}
