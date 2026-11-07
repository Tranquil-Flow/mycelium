// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/transports/iroh.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const IROH: &str = "mycelium_router/transports/iroh.py";
const DELIVERY: &str = "PyMyceliumRouterTransportsIrohIrohtransportInitDeliveryTimeoutSecondsL854";

fn at(rows: &[Bound], line: usize) -> &Bound {
    rows.iter()
        .find(|r| r.path == IROH && r.line == line && r.review.starts_with("partition-b-family:"))
        .unwrap_or_else(|| panic!("missing B family row {IROH}:{line}"))
}

#[test]
fn b_iroh_send_deadline_parameter_chain_keeps_the_original_delivery_allowance() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for line in [
        2156, 2181, 2184, 2190, 2235, 2644, 2101, 2233, 2240, 2642, 2649, 1909,
    ] {
        let r = at(&rows, line);
        assert_eq!(r.disposition, "derived");
        assert_eq!(r.unit, "milliseconds");
        assert_eq!(r.relation, DELIVERY, "line {line}");
        assert!(r.value.is_none() && r.budget.is_empty());
        assert!(proof::validate(r, &rows, &all), "line {line}");
        for wrong in [
            "CancelAndCleanup",
            "PyMyceliumUiGatewayConfigGatewayconfigUpstreamTimeoutSecondsL65",
        ] {
            let mut bad = r.clone();
            bad.relation = wrong.into();
            assert!(!proof::validate(&bad, &rows, &all), "wrong ID {line}");
        }
        let mut bad = r.clone();
        bad.expression.push_str(" + 1");
        assert!(!proof::validate(&bad, &rows, &all));
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_iroh_dual_caller_reconnect_needs_both_callers_reviewed_with_the_same_allowance() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let reconnect = at(&rows, 2644);
    assert!(proof::validate(reconnect, &rows, &all));
    // Removing the second caller's review (the recovery path at :2184) must reject the row.
    let without: Vec<Bound> = rows
        .iter()
        .filter(|r| !(r.path == IROH && r.line == 2184))
        .cloned()
        .collect();
    assert!(!proof::validate(reconnect, &without, &all));
    // A different relation on the second caller also rejects it.
    let mut changed = rows.clone();
    for r in &mut changed {
        if r.path == IROH && r.line == 2184 {
            r.relation = "CancelAndCleanup".into();
        }
    }
    assert!(!proof::validate(reconnect, &changed, &all));
}

#[test]
fn b_iroh_unrelated_deadline_carriers_are_not_borrowed_by_the_send_chain() {
    let rows = checked_in().unwrap();
    // The cancellation reconnect has an unreviewed second caller; owner-cleanup deadlines are
    // separate from the delivery allowance and stay pending.
    for line in [3112, 3117, 2849, 2975, 4325, 4393] {
        assert!(
            !rows.iter().any(|r| r.path == IROH
                && r.line == line
                && r.review
                    .starts_with("partition-b-family:forwarded-deadline")),
            "{IROH}:{line} must not be derived by the send-path deadline chain"
        );
    }
}
