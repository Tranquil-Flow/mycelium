// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-FAIL-11 (F16: gateway drain precedes registry cleanup and the final announcement; a failing drain still attempts every later effect); B-FAIL-05 (partial).
//!
//! O:F16 / L14-L15 production ordering, synthetic effects and virtual time.
use budgets::{BudgetId, ClockDomain, Entries, MonoInstant};
use runtime::shutdown::*;
struct Effects {
    now: MonoInstant,
    events: Vec<Phase>,
    drained: bool,
    fail_drain: bool,
    fail_registry: bool,
    reason: Option<StopReason>,
}
fn instant(t: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(1), t)
}
impl ShutdownEffects for Effects {
    fn now(&self) -> MonoInstant {
        self.now
    }
    fn cancel_chats(&mut self, r: StopReason) -> Result<(), &'static str> {
        self.events.push(Phase::CancelChats);
        self.reason = Some(r);
        Ok(())
    }
    fn close_listener(&mut self) -> Result<(), &'static str> {
        self.events.push(Phase::Listener);
        Ok(())
    }
    fn drain_gateway(&mut self, deadline: MonoInstant) -> Result<(), &'static str> {
        self.events.push(Phase::GatewayDrain);
        // Synthetic cleanup greater than router join, less than cancellation budget.
        self.now = instant(BudgetId::RouterClose.span(Entries::ZERO).unwrap().millis() + 1);
        assert!(self.now.ticks() < deadline.ticks());
        if self.fail_drain {
            Err("gateway_drain_failed")
        } else {
            self.drained = true;
            Ok(())
        }
    }
    fn close_registry(&mut self, _deadline: MonoInstant) -> Result<(), &'static str> {
        self.events.push(Phase::Registry);
        if !self.drained || self.fail_registry {
            Err("router_port_shutdown_cleanup_unproven")
        } else {
            Ok(())
        }
    }
    fn prove_targets(&mut self, _deadline: MonoInstant) -> Result<(), &'static str> {
        self.events.push(Phase::TargetProof);
        Ok(())
    }
    fn announce_stopped(&mut self) -> Result<(), &'static str> {
        self.events.push(Phase::Announcement);
        Ok(())
    }
}
fn effects() -> Effects {
    Effects {
        now: instant(0),
        events: vec![],
        drained: false,
        fail_drain: false,
        fail_registry: false,
        reason: None,
    }
}
fn deadline() -> MonoInstant {
    instant(0)
        .checked_add(BudgetId::PreviousClose.span(Entries::ZERO).unwrap())
        .unwrap()
}
#[test]
fn l14_l15_gateway_drain_precedes_registry_and_final_announcement() {
    for reason in [
        StopReason::Stop,
        StopReason::Rollover,
        StopReason::Failure("failed_run"),
    ] {
        let mut e = effects();
        let report = shutdown(&mut e, reason, deadline()).unwrap();
        assert!(report.proven(), "{:?}", report.failures);
        assert_eq!(e.reason, Some(reason));
        assert_eq!(
            e.events,
            vec![
                Phase::CancelChats,
                Phase::Listener,
                Phase::GatewayDrain,
                Phase::Registry,
                Phase::TargetProof,
                Phase::Announcement
            ]
        );
    }
    assert_eq!(StopReason::Stop.code(), "chat_cancelled");
    assert_eq!(StopReason::Rollover.code(), "chat_switching_runs");
}
#[test]
fn drain_failure_still_attempts_registry_target_proof_and_announcement() {
    let mut e = effects();
    e.fail_drain = true;
    let report = shutdown(&mut e, StopReason::Stop, deadline()).unwrap();
    assert!(!report.proven());
    assert_eq!(
        report.failures,
        vec![
            Failure {
                phase: Phase::GatewayDrain,
                code: "gateway_drain_failed"
            },
            Failure {
                phase: Phase::Registry,
                code: "router_port_shutdown_cleanup_unproven"
            }
        ]
    );
    assert_eq!(
        &e.events[3..],
        &[Phase::Registry, Phase::TargetProof, Phase::Announcement]
    );
}
