// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{BudgetId, ClockDomain, Entries, MonoInstant, Span, WallExpiry};
use planner::{Error, Result, admission::*, discovery::*, placement::Member};
use serde_json::Value;
fn now(t: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(1), t)
}
fn span(id: BudgetId) -> Span {
    id.span(Entries::ZERO).unwrap()
}
fn identity(n: &str) -> MemberIdentity {
    MemberIdentity {
        node_id: n.into(),
        generation: 1,
        incarnation: "boot".into(),
        endpoint_id: format!("ep-{n}"),
        verification_key_digest: format!("sha256:{}", "a".repeat(64)),
    }
}
struct Verified(VerifiedReport);
impl CapabilityVerifier for Verified {
    fn verify(&self, _: &[u8], _: &MemberIdentity, _: WallExpiry) -> Result<VerifiedReport> {
        Ok(self.0.clone())
    }
}
fn participants(case: &Value) -> Vec<AdmittedMember> {
    case["members"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            let member: Member = serde_json::from_value(row.clone()).unwrap();
            let id = identity(&member.node_id);
            if case["owner_node_id"] == member.node_id {
                local_owner(
                    id,
                    member,
                    span(BudgetId::WorkerDefaultWall),
                    now(3_600_000),
                    now(0),
                )
                .unwrap()
            } else {
                joined_member(
                    &Verified(VerifiedReport {
                        identity: id.clone(),
                        member,
                        expires_at: WallExpiry::from_unix_millis(3_600_000),
                    }),
                    b"authenticated",
                    &id,
                    span(BudgetId::WorkerDefaultWall),
                    WallExpiry::from_unix_millis(0),
                    now(0),
                )
                .unwrap()
            }
        })
        .collect()
}
fn inputs<'a>(f: &'a Value, c: &'a Value) -> PlanInputs<'a> {
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
        startup: Span::ZERO,
        service: span(BudgetId::UnknownMemberWall),
        cleanup: span(BudgetId::WorkerCleanupReserve),
    }
}
struct Proof(bool);
impl CleanupVerifier for Proof {
    fn verify(&self, _: &ReservationTicket) -> Result<()> {
        if self.0 {
            Ok(())
        } else {
            Err(Error::new("cleanup_unproven"))
        }
    }
}
#[test]
fn concurrent_reservations_are_atomic_and_cleanup_failure_keeps_capacity_held() {
    let f: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    let case = f["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["legacy_saved_selection"] == true && c.get("expected").is_some())
        .unwrap();
    let members = participants(case);
    let mut book = ReservationBook::default();
    let first = book
        .plan_and_reserve("run-1", inputs(&f, case), &members, now(0), phases())
        .unwrap();
    assert_eq!(book.active(), 1);
    assert!(
        book.plan_and_reserve("run-2", inputs(&f, case), &members, now(0), phases())
            .is_err()
    );
    assert_eq!(book.active(), 1);
    assert!(book.release(&first, &Proof(false)).is_err());
    assert_eq!(book.active(), 1);
    book.release(&first, &Proof(true)).unwrap();
    assert_eq!(book.active(), 0);
    assert!(book.release(&first, &Proof(true)).is_err());
    assert!(
        book.plan_and_reserve("run-2", inputs(&f, case), &members, now(0), phases())
            .is_ok()
    );
}
#[test]
fn short_member_window_refuses_before_any_reservation() {
    let f: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    let case = f["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["legacy_saved_selection"] == true && c.get("expected").is_some())
        .unwrap();
    let mut book = ReservationBook::default();
    let mut p = phases();
    p.compile = span(BudgetId::MemberChosenWall);
    let error = book
        .plan_and_reserve("run", inputs(&f, case), &participants(case), now(0), p)
        .unwrap_err();
    assert_eq!(error.code, "member_time_insufficient_for_share");
    assert_eq!(book.active(), 0);
}
#[test]
fn owner_binding_and_cleanup_reserve_cannot_be_removed_by_request_json() {
    let f: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    let case = f["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["legacy_saved_selection"] == true && c.get("expected").is_some())
        .unwrap();
    let members = participants(case);
    let mut book = ReservationBook::default();
    let mut bad = case.clone();
    bad["owner_node_id"] = serde_json::json!("a-member");
    assert_eq!(
        book.plan_and_reserve("run", inputs(&f, &bad), &members, now(0), phases())
            .unwrap_err()
            .code,
        "owner_capability_binding"
    );
    let mut p = phases();
    p.cleanup = Span::ZERO;
    assert_eq!(
        book.plan_and_reserve("run", inputs(&f, case), &members, now(0), p)
            .unwrap_err()
            .code,
        "planner_phase_budget_invalid"
    );
    assert_eq!(book.active(), 0);
}
#[test]
fn released_ticket_cannot_release_a_successor_and_drop_is_not_cleanup() {
    let f: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    let case = f["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["legacy_saved_selection"] == true && c.get("expected").is_some())
        .unwrap();
    let members = participants(case);
    let mut book = ReservationBook::default();
    let old = book
        .plan_and_reserve("same-id", inputs(&f, case), &members, now(0), phases())
        .unwrap();
    book.release(&old, &Proof(true)).unwrap();
    let new = book
        .plan_and_reserve("same-id", inputs(&f, case), &members, now(0), phases())
        .unwrap();
    assert_eq!(
        book.release(&old, &Proof(true)).unwrap_err().code,
        "unknown_reservation"
    );
    let mut foreign = ReservationBook::default();
    assert!(foreign.release(&new, &Proof(true)).is_err());
    drop(new);
    assert_eq!(book.active(), 1);
}
#[test]
fn measured_anchor_never_overrides_current_physical_disk_headroom() {
    let f: Value = serde_json::from_str(include_str!("fixtures/planning.json")).unwrap();
    let mut case = f["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| {
            c.get("expected").is_some()
                && f["models"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|m| m["model_id"] == c["model_id"] && !m["anchor"].is_null())
        })
        .unwrap()
        .clone();
    for row in case["members"].as_array_mut().unwrap() {
        row["caps"] =
            serde_json::json!({"storage_bytes":100_000_000_000u64,"max_wall_seconds":600});
        row["available_storage_bytes"] = serde_json::json!(0);
    }
    let mut book = ReservationBook::default();
    let e = book
        .plan_and_reserve(
            "run",
            inputs(&f, &case),
            &participants(&case),
            now(0),
            phases(),
        )
        .unwrap_err();
    assert_eq!(e.code, "member_disk_insufficient_for_share");
    assert_eq!(book.active(), 0);
}
#[test]
fn compile_budget_is_absolute_and_never_resets_at_subphase() {
    let window = CompileWindow::begin(now(0), now(123_000)).unwrap();
    assert_eq!(window.remaining(now(23_000)).unwrap().millis(), 100_000);
    assert!(window.remaining(now(123_000)).is_err());
    assert!(CompileWindow::begin(now(5), now(5)).is_err());
    assert!(
        window
            .remaining(MonoInstant::new(ClockDomain(2), 0))
            .is_err()
    );
}
