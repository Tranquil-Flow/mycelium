// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-FAIL-05 / B-ROLL-05 (target proof keeps the original outer deadline without restarting an allowance; an expired drain still attempts registry cleanup and the final announcement).
//!
use budgets::{BudgetId, ClockDomain, Entries, MonoInstant};
use runtime::shutdown::*;
struct Effects {
    now: MonoInstant,
    deadlines: Vec<(Phase, MonoInstant)>,
    announced: bool,
}
impl ShutdownEffects for Effects {
    fn now(&self) -> MonoInstant {
        self.now
    }
    fn cancel_chats(&mut self, _: StopReason) -> Result<(), &'static str> {
        Ok(())
    }
    fn close_listener(&mut self) -> Result<(), &'static str> {
        Ok(())
    }
    fn drain_gateway(&mut self, d: MonoInstant) -> Result<(), &'static str> {
        self.deadlines.push((Phase::GatewayDrain, d));
        Ok(())
    }
    fn close_registry(&mut self, d: MonoInstant) -> Result<(), &'static str> {
        self.deadlines.push((Phase::Registry, d));
        Ok(())
    }
    fn prove_targets(&mut self, d: MonoInstant) -> Result<(), &'static str> {
        self.deadlines.push((Phase::TargetProof, d));
        Ok(())
    }
    fn announce_stopped(&mut self) -> Result<(), &'static str> {
        self.announced = true;
        Ok(())
    }
}
#[test]
fn target_proof_keeps_original_outer_deadline_without_restarting_an_allowance() {
    let now = MonoInstant::new(ClockDomain(1), 0);
    let outer = now
        .checked_add(BudgetId::Compile.span(Entries::ZERO).unwrap())
        .unwrap();
    let mut e = Effects {
        now,
        deadlines: vec![],
        announced: false,
    };
    shutdown(&mut e, StopReason::Stop, outer).unwrap();
    assert_eq!(e.deadlines[2], (Phase::TargetProof, outer));
}
#[test]
fn expired_drain_still_attempts_registry_cleanup_and_final_announcement() {
    let now = MonoInstant::new(ClockDomain(1), 0);
    let mut e = Effects {
        now,
        deadlines: vec![],
        announced: false,
    };
    let report = shutdown(&mut e, StopReason::Stop, now).unwrap();
    assert_eq!(
        e.deadlines,
        vec![
            (Phase::GatewayDrain, now),
            (Phase::Registry, now),
            (Phase::TargetProof, now)
        ]
    );
    assert!(e.announced);
    assert!(!report.proven());
    assert_eq!(report.failures.len(), 3);
    assert!(
        report
            .failures
            .iter()
            .all(|f| f.code == "shutdown_deadline_expired")
    );
}
