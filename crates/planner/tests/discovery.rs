// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{AuthoritySource, BudgetId, ClockDomain, Entries, MonoInstant, Span, WallExpiry};
use planner::discovery::*;
use planner::placement::Member;
use planner::{Error, Result};
use serde_json::json;
fn mono(t: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(7), t)
}
fn identity(n: &str) -> MemberIdentity {
    MemberIdentity {
        node_id: n.into(),
        generation: 2,
        incarnation: "boot".into(),
        endpoint_id: "endpoint".into(),
        verification_key_digest: format!("sha256:{}", "a".repeat(64)),
    }
}
fn member(n: &str, wall: Option<u64>) -> Member {
    serde_json::from_value(json!({"node_id":n,"memory_bytes":16*1024*1024*1024u64,"caps":{"storage_bytes":2_000_000_000u64,"max_memory_bytes":4*1024*1024*1024u64,"max_wall_seconds":wall}})).unwrap()
}
struct Verifier {
    report: VerifiedReport,
    refuse: bool,
}
impl CapabilityVerifier for Verifier {
    fn verify(&self, _: &[u8], _: &MemberIdentity, _: WallExpiry) -> Result<VerifiedReport> {
        if self.refuse {
            Err(Error::new("signature_invalid"))
        } else {
            Ok(self.report.clone())
        }
    }
}
fn verifier(wall: Option<u64>) -> Verifier {
    Verifier {
        report: VerifiedReport {
            identity: identity("member"),
            expires_at: WallExpiry::from_unix_millis(2_000_000),
            member: member("member", wall),
        },
        refuse: false,
    }
}
// O:incident:F15b genuine owner, no fabricated standing grant.
#[test]
fn owner_and_member_deadlines_preserve_actual_caps_and_staggered_origins() {
    let proposal = BudgetId::WorkerDefaultWall.span(Entries::ZERO).unwrap();
    let owner = local_owner(
        identity("owner"),
        member("owner", None),
        proposal,
        mono(2_000_000),
        mono(0),
    )
    .unwrap();
    let joined = joined_member(
        &verifier(Some(600)),
        b"verified-envelope",
        &identity("member"),
        proposal,
        WallExpiry::from_unix_millis(0),
        mono(0),
    )
    .unwrap();
    assert_eq!(owner.wall(), proposal);
    assert_eq!(owner.source(), AuthoritySource::LocalProposal);
    assert_eq!(
        joined.wall(),
        BudgetId::MemberChosenWall.span(Entries::ZERO).unwrap()
    );
    let deadlines =
        worker_deadlines(&[owner, joined], &[mono(0), mono(10_000)], mono(10_000)).unwrap();
    assert_eq!(deadlines.stop_by.ticks(), 600_000);
    assert_eq!(deadlines.ceiling_at.ticks(), 580_000);
    assert_eq!(deadlines.rollover_at.ticks(), 490_000);
}
#[test]
fn unknown_member_fallback_is_not_owner_authority() {
    let proposal = BudgetId::WorkerDefaultWall.span(Entries::ZERO).unwrap();
    let joined = joined_member(
        &verifier(None),
        b"signed",
        &identity("member"),
        proposal,
        WallExpiry::from_unix_millis(0),
        mono(0),
    )
    .unwrap();
    assert_eq!(
        joined.wall(),
        BudgetId::UnknownMemberWall.span(Entries::ZERO).unwrap()
    );
    assert_eq!(joined.source(), AuthoritySource::UnknownMemberFallback);
}
#[test]
fn rejected_signature_stale_report_binding_and_expiry_cannot_enter_planner() {
    let proposal = BudgetId::WorkerDefaultWall.span(Entries::ZERO).unwrap();
    for mutation in [
        "signature",
        "generation",
        "endpoint",
        "key",
        "expiry",
        "node",
    ] {
        let mut v = verifier(Some(600));
        match mutation {
            "signature" => v.refuse = true,
            "generation" => v.report.identity.generation += 1,
            "endpoint" => v.report.identity.endpoint_id = "foreign".into(),
            "key" => v.report.identity.verification_key_digest = "foreign".into(),
            "expiry" => v.report.expires_at = WallExpiry::from_unix_millis(0),
            "node" => v.report.member.node_id = "foreign".into(),
            _ => unreachable!(),
        };
        assert!(
            joined_member(
                &v,
                b"signed",
                &identity("member"),
                proposal,
                WallExpiry::from_unix_millis(0),
                mono(0)
            )
            .is_err(),
            "{mutation}"
        );
    }
}
#[test]
fn expired_and_cross_clock_windows_refuse() {
    let owner = local_owner(
        identity("owner"),
        member("owner", None),
        Span::from_millis(1_800_000),
        mono(2_000_000),
        mono(0),
    )
    .unwrap();
    assert!(owner.current(mono(2_000_000)).is_err());
    assert!(worker_deadlines(&[owner], &[MonoInstant::new(ClockDomain(8), 0)], mono(0)).is_err());
}
