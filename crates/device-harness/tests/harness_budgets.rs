// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{Fault, FaultKind, Result, harness_budgets::*, soak::*};
#[test]
fn l1_admits_exact_harness_policies_without_promoting_clock_observations() {
    for (name, expected_ms) in [
        ("HarnessMinimumSoak", 1_200_000),
        ("KeeperRecoveryWindow", 21_600_000),
        ("KeeperRolloverGrace", 300_000),
        ("KeeperStartupGrace", 900_000),
        ("KeeperMemberSilence", 300_000),
        ("HarnessOwnerStatusCheck", 20_000),
    ] {
        assert_eq!(L1.span(name).expect(name).millis(), expected_ms, "{name}");
        let entry = budgets::BudgetId::from_name(name).unwrap().entry();
        assert!(matches!(
            entry.provenance,
            budgets::table::Provenance::Source(_)
        ));
    }
    for name in [
        "HarnessFixtureKeeperEpoch",
        "HarnessFixtureKeeperHealthyAt",
        "HarnessFixtureKeeperSilenceAt",
    ] {
        assert!(
            budgets::BudgetId::from_name(name).is_none(),
            "clock observations are not policy: {name}"
        );
    }
    assert_eq!(L1.count("KeeperRecoveryAttempts").unwrap(), 3);
    assert_eq!(minimum_soak(&L1).unwrap().millis(), 1_200_000);
    let limits = KeeperLimits::from_registry(&L1).unwrap();
    assert!(limits.rollover_grace < limits.startup_grace);
    assert!(limits.startup_grace < limits.recovery_window);
    let mut p = policy();
    bind_soak(&L1, &mut p).unwrap();
    assert_eq!(p.minimum_ms, 1_200_000);
    p.minimum_ms = 2_700_000;
    bind_soak(&L1, &mut p).unwrap();
    assert_eq!(p.minimum_ms, 2_700_000);
}

struct RecordedRegistry;
impl Registry for RecordedRegistry {
    fn span(&self, name: &str) -> Result<Span> {
        Ok(Span::from_millis(match name {
            "HarnessMinimumSoak" => 1_200_000,
            "KeeperRecoveryWindow" => 21_600_000,
            "KeeperRolloverGrace" | "KeeperMemberSilence" => 300_000,
            "KeeperStartupGrace" => 900_000,
            _ => panic!("unrequested registry name: {name}"),
        }))
    }
    fn count(&self, name: &str) -> Result<u64> {
        assert_eq!(name, "KeeperRecoveryAttempts");
        Ok(3)
    }
    fn bytes(&self, _: &str) -> Result<budgets::Bytes> {
        panic!("byte caps are not part of this fixture")
    }
}
fn policy() -> Policy {
    Policy {
        requested_ms: 600_000,
        minimum_ms: 1,
        chat_every_ms: 150_000,
        sample_every_ms: 60_000,
        mailbox_every_ms: 600_000,
        visitor_every_ms: 600_000,
        restart_at_ms: Some(300_000),
        minimum_rollovers: 1,
        mailbox_max_bytes: Some(10),
        restart_expectation: RestartExpectation::IdleUntilExplicitStart,
        required_steps: Default::default(),
    }
}
#[test]
fn owner_ruled_floor_is_registry_bound_even_when_requested_or_injected_floor_is_shorter() {
    let mut p = policy();
    bind_soak(&RecordedRegistry, &mut p).unwrap();
    assert_eq!(p.minimum_ms, 1_200_000);
    let e = Evidence {
        duration_ms: 1_199_000,
        ..Default::default()
    };
    assert!(
        evaluate(&p, &e)
            .reasons
            .contains(&"duration_incomplete".into())
    );
    let e = Evidence {
        duration_ms: 1_200_000,
        ..Default::default()
    };
    assert!(
        !evaluate(&p, &e)
            .reasons
            .contains(&"duration_incomplete".into())
    );
    p.minimum_ms = 2_700_000;
    bind_soak(&RecordedRegistry, &mut p).unwrap();
    assert_eq!(p.minimum_ms, 2_700_000); // no weakening a final-run requirement
}
#[test]
fn semantic_registry_requests_have_no_literal_or_equal_value_fallback() {
    struct Missing;
    impl Registry for Missing {
        fn span(&self, _: &str) -> Result<Span> {
            Err(Fault::new("missing_fixture_budget", FaultKind::Admission))
        }
        fn count(&self, _: &str) -> Result<u64> {
            self.span("").map(|_| 0)
        }
        fn bytes(&self, _: &str) -> Result<budgets::Bytes> {
            self.span("").map(|_| budgets::Bytes::ZERO)
        }
    }
    assert_eq!(
        bind_soak(&Missing, &mut policy()).unwrap_err().code,
        "missing_fixture_budget"
    );
    assert!(KeeperLimits::from_registry(&Missing).is_err());
    assert_eq!(
        L1.span("UnregisteredHarnessTestBudget").unwrap_err().code,
        "harness_budget_unregistered"
    );
    let l = KeeperLimits::from_registry(&RecordedRegistry).unwrap();
    assert_eq!(l.recovery_window.millis(), 21_600_000);
    assert_eq!(l.attempts, 3);
    assert_eq!(l.rollover_grace.millis(), 300_000);
    assert_eq!(l.startup_grace.millis(), 900_000);
    assert_eq!(l.member_silence.millis(), 300_000);
    assert_eq!(
        L1.span("PyHelperMyceliumRouterA10BatchAdmissionL171C9")
            .unwrap_err()
            .code,
        "harness_budget_wrong_unit"
    );
}
