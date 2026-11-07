// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-ROLL-08 (F13: rollover renews the same resource and origin up to the immutable cap); B-FAIL-11 (a failed stop is not success).
//!
//! O:F13 model-resource composition; no tunnel, socket or subprocess.
use budgets::{BudgetId, ClockDomain, Entries, MonoInstant, RelayAction, ServingProbe};
use runtime::relay::*;
use std::{cell::Cell, rc::Rc};
struct Resource {
    stops: Rc<Cell<usize>>,
    fail: Rc<Cell<bool>>,
}
impl RelayResource for Resource {
    fn stop(&mut self) -> Result<(), &'static str> {
        self.stops.set(self.stops.get() + 1);
        if self.fail.get() {
            Err("relay_cleanup_unproven")
        } else {
            Ok(())
        }
    }
}
fn instant(t: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(1), t)
}
fn fixture() -> (QuickRelay<Resource>, Rc<Cell<usize>>, Rc<Cell<bool>>) {
    let stops = Rc::new(Cell::new(0));
    let fail = Rc::new(Cell::new(false));
    (
        QuickRelay::new(
            instant(0),
            "https://fixture.invalid".into(),
            Resource {
                stops: stops.clone(),
                fail: fail.clone(),
            },
        )
        .unwrap(),
        stops,
        fail,
    )
}
#[test]
fn f13_rollover_renews_same_resource_and_origin_to_immutable_cap() {
    let (mut relay, stops, _) = fixture();
    let lead = BudgetId::RelayRenewalLead
        .span(Entries::ZERO)
        .unwrap()
        .millis();
    let window = BudgetId::RelayWindow.span(Entries::ZERO).unwrap().millis();
    let cap = BudgetId::RelayGrantedWindowCap
        .span(Entries::ZERO)
        .unwrap()
        .millis();
    let original = relay.origin().to_owned();
    for _ in 1..cap / window {
        let at = instant(relay.deadline().ticks() - lead);
        assert_eq!(
            relay.step(at, ServingProbe::Serving).unwrap(),
            RelayAction::Renewed
        );
        assert_eq!(relay.origin(), original);
        assert_eq!(stops.get(), 0);
    }
    assert_eq!(relay.granted().millis(), cap);
    assert_eq!(
        relay
            .step(
                instant(relay.deadline().ticks() - lead),
                ServingProbe::Serving
            )
            .unwrap(),
        RelayAction::CapReached
    );
    assert_eq!(
        relay.step(relay.deadline(), ServingProbe::Serving).unwrap(),
        RelayAction::Closed
    );
    assert_eq!(stops.get(), 1);
    assert!(relay.clean());
    relay.stop().unwrap();
    assert_eq!(stops.get(), 1);
}
#[test]
fn failed_probe_does_not_renew_and_failed_stop_is_not_success() {
    for probe in [ServingProbe::Failed, ServingProbe::NotServing] {
        let (mut relay, stops, fail) = fixture();
        let deadline = relay.deadline();
        let lead = BudgetId::RelayRenewalLead
            .span(Entries::ZERO)
            .unwrap()
            .millis();
        assert_eq!(
            relay.step(instant(deadline.ticks() - lead), probe).unwrap(),
            RelayAction::Waiting
        );
        assert_eq!(relay.deadline(), deadline);
        fail.set(true);
        assert_eq!(
            relay.step(deadline, probe).unwrap_err(),
            RelayError::Cleanup("relay_cleanup_unproven")
        );
        assert!(!relay.clean());
        fail.set(false);
        relay.stop().unwrap();
        assert!(relay.clean());
        assert_eq!(stops.get(), 2);
    }
}
