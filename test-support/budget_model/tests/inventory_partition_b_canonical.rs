// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/transports/iroh.py; O:path:mycelium_router/a10_fault_negatives.py.
use budget_model::inventory::{checked_in, validate};

#[test]
fn canonical_b_guard_checks_families_quantities_and_fixture_scopes() {
    let rows = checked_in().unwrap();
    assert_eq!(validate(&rows), Ok(()));
    for (path, line, claim, replacement) in [
        (
            "mycelium_router/transports/iroh.py",
            3287,
            "partition-b-family:",
            "RegistryPoll",
        ),
        (
            "mycelium_speculative_decode.py",
            394,
            "partition-b-family:",
            "PyFinalMyceliumSpeculativeDecodeCircuitBreakerRollingWindowCyclesL81C0",
        ),
        (
            "mycelium_iroh_sidecar/client.py",
            37,
            "partition-b-quantity:",
            "PyThresholdPhysicalInferenceNodeL883C0",
        ),
        (
            "mycelium_router/a10_fault_negatives.py",
            352,
            "partition-b-fixture:",
            "",
        ),
    ] {
        let original = rows
            .iter()
            .find(|r| r.path == path && r.line == line && r.review.starts_with(claim))
            .expect("accepted B family representative");
        let mut changed = rows.clone();
        let row = changed.iter_mut().find(|r| r.key == original.key).unwrap();
        row.review = format!("{claim}unreviewed-rule; reject unknown rule");
        assert!(
            validate(&changed).is_err(),
            "unknown claim {}",
            original.key
        );
        if !replacement.is_empty()
            && original.relation != replacement
            && original.budget != replacement
        {
            let mut changed = rows.clone();
            let row = changed.iter_mut().find(|r| r.key == original.key).unwrap();
            if row.budget.is_empty() {
                row.relation = replacement.into();
            } else {
                row.budget = replacement.into();
            }
            assert!(
                validate(&changed).is_err(),
                "wrong same-unit ID {}",
                original.key
            );
        }
    }
}
