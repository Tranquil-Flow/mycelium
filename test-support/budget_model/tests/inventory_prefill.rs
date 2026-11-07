// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/transports/iroh.py.
use budget_model::inventory::{checked_in, validate};
use budgets::{
    BudgetId,
    table::{Phase, Provenance, Purpose},
};

#[test]
fn prefill_factors_are_dimensionless_and_rtt_fallback_is_not_measured() {
    for (name, expected) in [
        ("PyPrefillStallRttMultiplier", 8),
        ("PyPrefillStallTransferMultiplier", 4),
    ] {
        let id = BudgetId::from_name(name).expect("missing dimensionless policy factor");
        assert_eq!(id.count().unwrap().get(), expected);
        assert!(
            id.span(budgets::Entries::new(0)).is_err(),
            "a multiplier is not a duration"
        );
        assert!(matches!(id.entry().provenance, Provenance::Source(_)));
    }
    let id = BudgetId::from_name("PyPrefillUnmeasuredRttFallback").expect("missing fallback");
    assert_eq!(id.span(budgets::Entries::new(0)).unwrap().millis(), 1000);
    assert_eq!(
        id.entry().purpose,
        Purpose::Operational(Phase::Unclassified)
    );
    assert!(matches!(id.entry().provenance, Provenance::Source(_)));
}

#[test]
fn spent_stall_windows_remain_linked_to_the_resend_ceiling() {
    let rows = checked_in().unwrap();
    for line in [1667, 1718, 1774, 1931, 2074, 2105, 2125] {
        let row = rows
            .iter()
            .find(|r| r.path == "mycelium_router/transports/iroh.py" && r.line == line)
            .unwrap_or_else(|| panic!("missing consumption site {line}"));
        assert_eq!(row.disposition, "derived");
        assert_eq!(row.relation, "PyPrefillMaximumResends");
        assert_eq!(row.unit, "entries");
        assert!(row.value.is_none());
        let mut changed = rows.clone();
        let bad = changed.iter_mut().find(|r| r.key == row.key).unwrap();
        bad.relation = "RenewalDivisor".into();
        assert!(
            validate(&changed).is_err(),
            "same-unit wrong ceiling must fail"
        );
        let mut changed = rows.clone();
        let bad = changed.iter_mut().find(|r| r.key == row.key).unwrap();
        bad.expression = "0".into();
        bad.scope = "unreviewed".into();
        assert!(validate(&changed).is_err());
    }
}

#[test]
fn prefill_review_cannot_waive_an_unrelated_wait() {
    let mut rows = checked_in().unwrap();
    let row = rows
        .iter_mut()
        .find(|r| r.unit == "milliseconds" && r.path != "mycelium_router/transports/iroh.py")
        .unwrap();
    row.review = "prefill-category:consumed-resends; forged".into();
    assert!(validate(&rows).is_err());
}
